
import logging
from flask import Flask, request, jsonify
import numpy as np
import time
from collections import deque
from lobstr import LoBSTrInference  # Import LoBSTr

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLServer")

# --- GLOBAL STATE ---
history_buffer = deque(maxlen=60)

# Initialize LoBSTr (Relative Path)
lobstr_model = LoBSTrInference('models/lobstr_gru_60.pth')

class DynamicHeightEstimator:
    def __init__(self):
        self.standard_height = 1.70
        self.estimated_height = 1.70
        self.is_calibrating = False
        self.head_history = []
        
    def start_calibration(self):
        self.is_calibrating = True
        self.head_history = []
        self.estimated_height = 1.70
        logger.info("Calibration Started")

    def update(self, head_pos):
        if self.is_calibrating and head_pos is not None:
            self.head_history.append(head_pos[1])

    def finish_calibration(self):
        self.is_calibrating = False
        if not self.head_history:
            return 1.70
        max_h = np.percentile(self.head_history, 95)
        est = max_h + 0.10
        if 1.4 < est < 2.2:
            self.estimated_height = est
        else:
            self.estimated_height = 1.70
        return self.estimated_height

    def get_scale_factor(self):
        return self.standard_height / self.estimated_height

    def set_manual_height(self, height):
        self.is_calibrating = False
        if 1.0 < height < 2.5:
            self.estimated_height = height
            logger.info(f"Manual Height Set: {height}")
        else:
            logger.warning(f"Invalid manual height: {height}")

height_estimator = DynamicHeightEstimator()

def extract_transform_from_matrix(matrix_16):
    if not matrix_16:
        # Default Identity-ish
        return (np.array([0., 0., 0.]), np.array([0., 0., -1.]), 
                np.array([1., 0., 0.]), np.array([0., 1., 0.]), np.eye(4))
    
    # Handle Full Hand Skeleton (432 floats) -> Take first 16 (Wrist)
    if len(matrix_16) > 16:
        matrix_16 = matrix_16[:16]
    
    if len(matrix_16) < 16:
         return (np.array([0., 0., 0.]), np.array([0., 0., -1.]), 
                np.array([1., 0., 0.]), np.array([0., 1., 0.]), np.eye(4))

    m_col_major = np.array(matrix_16, dtype=float).reshape(4, 4).T
    
    # Extract components
    position = m_col_major[:3, 3]
    right = m_col_major[:3, 0]
    up = m_col_major[:3, 1]
    forward = -m_col_major[:3, 2] # Vision Forward is -Z
    
    return position, forward, right, up, m_col_major

def calculate_shoulders_from_head_matrix(head_pos, head_right, user_height=1.70, shoulder_width=0.40):
    shoulder_y_offset = user_height * 0.135
    left_shoulder = head_pos - head_right * (shoulder_width / 2)
    left_shoulder[1] -= shoulder_y_offset
    right_shoulder = head_pos + head_right * (shoulder_width / 2)
    right_shoulder[1] -= shoulder_y_offset
    return left_shoulder, right_shoulder

class SimpleFABRIK:
    def __init__(self, user_height=1.70):
        self.user_height = user_height
        self.update_arm_lengths()

    def update_arm_lengths(self, height=None):
        if height: self.user_height = height
        self.upper_arm = self.user_height * 0.19
        self.forearm = self.user_height * 0.18

    def solve_arm_with_natural_bend(self, shoulder, wrist, prev_elbow=None):
        shoulder = np.array(shoulder)
        wrist = np.array(wrist)
        dir_vec = wrist - shoulder
        dist = np.linalg.norm(dir_vec)
        max_reach = self.upper_arm + self.forearm
        
        if dist < 1e-6: return (shoulder + np.array([0, -0.2, 0])).tolist()
        
        if dist > max_reach * 0.99:
            return (shoulder + dir_vec * (self.upper_arm / dist)).tolist()
        
        numerator = (self.upper_arm**2 + dist**2 - self.forearm**2)
        denominator = (2 * self.upper_arm * dist)
        cos_angle = np.clip(numerator / denominator, -1.0, 1.0)
        sin_angle = np.sqrt(1.0 - cos_angle**2)
        
        elbow_base = shoulder + dir_vec * (self.upper_arm / dist)
        
        if prev_elbow is not None:
            to_prev = np.array(prev_elbow) - elbow_base
            norm_prev = np.linalg.norm(to_prev)
            bend_dir = to_prev / norm_prev if norm_prev > 1e-6 else np.array([0., -0.2, -0.2])
        else:
            bend_dir = np.array([0., -0.2, -0.2])
            bend_dir = bend_dir / np.linalg.norm(bend_dir)
        
        perp_len = sin_angle * self.upper_arm
        return (elbow_base + bend_dir * perp_len).tolist()

ik_solver = SimpleFABRIK(user_height=1.70)
global_prev_pelvis = None
global_prev_left_elbow = None
global_prev_right_elbow = None

def estimate_pelvis_com_v2(head_pos, l_pos, r_pos, user_height):
    pelvis_y = head_pos[1] - (user_height * 0.44)
    if l_pos is not None and r_pos is not None:
        hand_avg = (l_pos + r_pos) / 2
        pelvis_x = head_pos[0] * 0.8 + hand_avg[0] * 0.2
        pelvis_z = head_pos[2] * 0.8 + hand_avg[2] * 0.2
    else:
        pelvis_x = head_pos[0]
        pelvis_z = head_pos[2]
    return np.array([pelvis_x, pelvis_y, pelvis_z])

# ... (existing imports/funcs) ...

@app.route('/calibrate/start', methods=['POST'])
def calibrate_start():
    height_estimator.start_calibration()
    return jsonify({"status": "started"})

@app.route('/calibrate/finish', methods=['POST'])
def calibrate_finish():
    height = height_estimator.finish_calibration()
    ik_solver.update_arm_lengths(height)
    return jsonify({"status": "finished", "height": height})

@app.route('/calibrate/manual', methods=['POST'])
def calibrate_manual():
    try:
        data = request.json
        height = float(data.get('height', 1.70))
        height_estimator.set_manual_height(height)
        ik_solver.update_arm_lengths(height)
        return jsonify({"status": "success", "height": height_estimator.estimated_height})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# ... (existing imports) ...
import json
import os
from datetime import datetime

# ... (Global State) ...

class DataRecorder:
    def __init__(self):
        self.is_recording = False
        self.start_time = 0
        self.data_buffer = []
        self.duration = 5.0
        self.output_dir = "recordings"
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def start(self, duration=5.0):
        self.is_recording = True
        self.start_time = time.time()
        self.duration = duration
        self.data_buffer = []
        logger.info(f"Started Recording for {duration}s")

    def update(self, input_data, output_skeleton):
        if not self.is_recording:
            return

        elapsed = time.time() - self.start_time
        frame_data = {
            "timestamp": time.time(),
            "elapsed": elapsed,
            "input": input_data,
            "output": output_skeleton
        }
        self.data_buffer.append(frame_data)

        if elapsed >= self.duration:
            self.save()

    def save(self):
        self.is_recording = False
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.output_dir}/skeleton_data_{timestamp}.json"
        try:
            # Helper to convert numpy arrays to list
            def np_converter(obj):
                if isinstance(obj, np.integer): return int(obj)
                elif isinstance(obj, np.floating): return float(obj)
                elif isinstance(obj, np.ndarray): return obj.tolist()
                return obj

            with open(filename, 'w') as f:
                json.dump(self.data_buffer, f, default=np_converter, indent=2)
            
            logger.info(f"Recording saved to {filename} ({len(self.data_buffer)} frames)")
        except Exception as e:
            logger.error(f"Failed to save recording: {e}")

recorder = DataRecorder()

# ... (existing routes) ...

@app.route('/record/start', methods=['POST'])
def record_start():
    recorder.start(duration=5.0)
    return jsonify({"status": "started", "message": "Recording 5 seconds of data..."})

@app.route('/predict', methods=['POST'])
def predict():
    global global_prev_pelvis, global_prev_left_elbow, global_prev_right_elbow
    start_time = time.time()
    try:
        data = request.json
        
        # 1. Parse Input Transforms
        # Support both flat and nested 'raw_udp' structure
        input_source = data
        if 'raw_udp' in data:
            input_source = data['raw_udp']

        # Need raw matrices for LoBSTr (if available)
        head_matrix_raw = input_source.get('head', [])
        l_hand_matrix_raw = input_source.get('leftHand', [])
        r_hand_matrix_raw = input_source.get('rightHand', [])
        
        head_pos, head_fwd, head_right, head_up, head_mat = extract_transform_from_matrix(head_matrix_raw)
        l_pos, _, _, _, l_mat = extract_transform_from_matrix(l_hand_matrix_raw)
        r_pos, _, _, _, r_mat = extract_transform_from_matrix(r_hand_matrix_raw)
        
        # ... (Processing Logic) ...

        
        # 2. Heuristic Height Scale
        height_estimator.update(head_pos)
        scale = height_estimator.get_scale_factor()
        
        # Scaled Positions (For visual output and heuristic IK)
        head_pos_k = head_pos * scale
        l_pos_k = l_pos * scale
        r_pos_k = r_pos * scale
        
        # 3. Pelvis Heuristic (Input for LoBSTr)
        pelvis_pos_k = estimate_pelvis_com_v2(head_pos_k, l_pos_k, r_pos_k, height_estimator.estimated_height)
        
        # Smooth Pelvis
        if global_prev_pelvis is not None:
             pelvis_pos_k = pelvis_pos_k * 0.7 + np.array(global_prev_pelvis) * 0.3
        global_prev_pelvis = pelvis_pos_k.tolist()
        
        # 4. Construct Hips Matrix (Upright)
        look_dir = head_fwd.copy()
        look_dir[1] = 0
        norm_look = np.linalg.norm(look_dir)
        if norm_look > 1e-6:
            look_dir /= norm_look
        else:
            look_dir = np.array([0, 0, -1]) 
        
        hips_z = -look_dir # Vision Forward is -Z
        hips_y = np.array([0., 1., 0.])
        hips_x = np.cross(hips_y, hips_z)
        
        hips_mat = np.eye(4)
        hips_mat[:3, 0] = hips_x
        hips_mat[:3, 1] = hips_y
        hips_mat[:3, 2] = hips_z
        hips_mat[:3, 3] = pelvis_pos_k
        
        # Scale Matrices (Translations only)
        # Note: Rotations unchanged.
        hips_mat_s = hips_mat.copy() 
        head_mat_s = head_mat.copy(); head_mat_s[:3, 3] *= scale
        l_mat_s = l_mat.copy(); l_mat_s[:3, 3] *= scale
        r_mat_s = r_mat.copy(); r_mat_s[:3, 3] *= scale
        
        # Update Buffer
        history_buffer.append({
            'Hips': hips_mat_s,
            'Head': head_mat_s,
            'LeftHand': l_mat_s,
            'RightHand': r_mat_s
        })
        
        # 5. Run LoBSTr Logic if buffer full
        legs_result = None
        if len(history_buffer) == 60:
            world_mats, contact = lobstr_model.predict(list(history_buffer))
            if world_mats is not None:
                # Indices: 1:LKnee, 2:LAnkle, 5:RKnee, 6:RAnkle
                l_knee_pos = world_mats[1][:3, 3]
                l_ankle_pos = world_mats[2][:3, 3]
                r_knee_pos = world_mats[5][:3, 3]
                r_ankle_pos = world_mats[6][:3, 3]
                
                legs_result = {
                    "leftKnee": l_knee_pos,
                    "leftAnkle": l_ankle_pos,
                    "rightKnee": r_knee_pos,
                    "rightAnkle": r_ankle_pos
                }

        # 6. Upper Body IK (Heuristic)
        left_shoulder, right_shoulder = calculate_shoulders_from_head_matrix(
            head_pos_k, head_right, 
            user_height=height_estimator.estimated_height,
            shoulder_width=0.40
        )
        l_elbow = ik_solver.solve_arm_with_natural_bend(left_shoulder, l_pos_k, global_prev_left_elbow)
        r_elbow = ik_solver.solve_arm_with_natural_bend(right_shoulder, r_pos_k, global_prev_right_elbow)
        global_prev_left_elbow = l_elbow
        global_prev_right_elbow = r_elbow
        
        # 7. Fallback Legs (Heuristic) if LoBSTr not ready or failed
        if legs_result is None:
             leg_width = 0.1
             l_foot = [pelvis_pos_k[0] - leg_width, 0.0, pelvis_pos_k[2]]
             r_foot = [pelvis_pos_k[0] + leg_width, 0.0, pelvis_pos_k[2]]
             knee_y = pelvis_pos_k[1] * 0.5
             l_knee = [pelvis_pos_k[0] - leg_width, knee_y, pelvis_pos_k[2] + 0.05]
             r_knee = [pelvis_pos_k[0] + leg_width, knee_y, pelvis_pos_k[2] + 0.05]
             legs_result = {
                 "leftKnee": l_knee, "leftAnkle": l_foot,
                 "rightKnee": r_knee, "rightAnkle": r_foot
             }

        # 8. Denormalize & Output
        inv_scale = 1.0 / scale
        def denorm(vec): return (np.array(vec) * inv_scale).tolist()
        
        skeleton = {
            "head": head_pos.tolist(),
            "neck": denorm([head_pos_k[0], head_pos_k[1] - 0.15, head_pos_k[2]]),
            "spine": denorm([pelvis_pos_k[0], pelvis_pos_k[1] + 0.3, pelvis_pos_k[2]]),
            "pelvis": denorm(pelvis_pos_k),
            
            "leftShoulder": denorm(left_shoulder),
            "rightShoulder": denorm(right_shoulder),
            "leftElbow": denorm(l_elbow),
            "rightElbow": denorm(r_elbow),
            "leftHand": denorm(l_pos_k),
            "rightHand": denorm(r_pos_k),
            
            "leftKnee": denorm(legs_result["leftKnee"]),
            "rightKnee": denorm(legs_result["rightKnee"]),
            "leftAnkle": denorm(legs_result["leftAnkle"]), 
            "rightAnkle": denorm(legs_result["rightAnkle"]),
            
            "ml_latency_ms": (time.time() - start_time) * 1000,
            "height_scale": scale,
            "is_calibrating": height_estimator.is_calibrating,
            "calibrated_height": height_estimator.estimated_height,
            "lobstr_active": len(history_buffer) == 60
        }
        
        # Record Data if active
        recorder.update(data, skeleton)
        
        return jsonify(skeleton)

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
