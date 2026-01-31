import logging
from flask import Flask, request, jsonify
from collections import deque
import numpy as np
import time

# Initialize Flask
app = Flask(__name__)

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLServer")

# --- DYNAMIC HEIGHT ESTIMATOR ---
class DynamicHeightEstimator:
    def __init__(self):
        self.standard_height = 1.70 # Reference height (m)
        self.estimated_height = 1.70 # Default
        self.is_calibrating = False # State flag
        self.head_history = []
        
    def start_calibration(self):
        self.is_calibrating = True
        self.head_history = []
        self.estimated_height = 1.70 # Reset to default during calibration
        logger.info("Calibration Started")

    def update(self, head_pos):
        # Only collect data if calibrating
        if self.is_calibrating:
             self.head_history.append(head_pos[1])

    def finish_calibration(self):
        self.is_calibrating = False
        if not self.head_history:
             logger.warning("Calibration finished but no data collected. Using default 1.7m.")
             return 1.70
             
        # Robust Estimation: Eye Level + 0.10m
        max_h = np.percentile(self.head_history, 95)
        est = max_h + 0.10
        
        # Sanity Check
        if 1.4 < est < 2.2:
             self.estimated_height = est
             logger.info(f"Calibration Finished. Estimated Height: {est:.2f}m")
        else:
             logger.warning(f"Estimated height {est:.2f}m out of bounds. Using default 1.7m.")
             self.estimated_height = 1.70
             
        return self.estimated_height
    
    def get_scale_factor(self):
        return self.standard_height / self.estimated_height

height_estimator = DynamicHeightEstimator()

# --- FABRIK IK CLASS (Dynamic Arms) ---
class SimpleFABRIK:
    def __init__(self, user_height=1.70):
        self.user_height = user_height
        self.update_arm_lengths()

    def update_arm_lengths(self, height=None):
        if height:
            self.user_height = height
        # Arm lengths scaled by user height
        # Standard Proportions: Upper Arm ~ 19%, Forearm ~ 18%
        self.upper_arm = self.user_height * 0.19
        self.forearm = self.user_height * 0.18
        logger.info(f"Updated Arm Lengths for Height {self.user_height:.2f}m: Upper={self.upper_arm:.2f}, Forearm={self.forearm:.2f}")

    def solve_arm(self, shoulder, wrist):
        shoulder = np.array(shoulder)
        wrist = np.array(wrist)
        
        dir_vec = wrist - shoulder
        dist = np.linalg.norm(dir_vec)
        
        # Safety: avoid division by zero
        if dist < 1e-6:
             return (shoulder + np.array([0, -0.2, 0])).tolist()
        
        max_reach = self.upper_arm + self.forearm
        
        # Case 1: Target unreachable (Too far) -> Fully extend
        if dist > max_reach * 0.99:
            elbow = shoulder + dir_vec * (self.upper_arm / dist)
        else:
            # Case 2: Reachable -> Triangle solution (Cosine Rule)
            # a = upper_arm, b = forearm, c = dist
            numerator = (self.upper_arm**2 + dist**2 - self.forearm**2)
            denominator = (2 * self.upper_arm * dist)
            cos_angle = numerator / denominator
            cos_angle = np.clip(cos_angle, -1, 1)
            
            # Elbow base position on the line between shoulder and wrist
            elbow_base = shoulder + dir_vec * (self.upper_arm / dist)
            
            # Calculate height of triangle
            sin_angle = np.sqrt(1 - cos_angle**2)
            
            # Lift elbow upwards (and slightly outwards) to simulate natural bend
            # User heuristic: 0.2 factor
            perpendicular_height = sin_angle * self.upper_arm * 0.2
            
            elbow = elbow_base
            elbow[1] += perpendicular_height 
             
        return elbow.tolist()

# --- HELPER FUNCTIONS ---

def estimate_pelvis_com_v2(head, left_hand, right_hand, prev_pelvis=None):
    head = np.array(head)
    lh = np.array(left_hand)
    rh = np.array(right_hand)
    
    # Hand Average
    hand_avg = (lh + rh) / 2
    
    # 1. Pelvis Y: Head Y - 0.75m (Revised Proportions)
    pelvis_y = head[1] - 0.75
    
    # 2. Pelvis X/Z: Mix of Head (80%) and Hands (20%)
    pelvis_x = head[0] * 0.8 + hand_avg[0] * 0.2
    pelvis_z = head[2] * 0.8 + hand_avg[2] * 0.2
    
    pelvis = np.array([pelvis_x, pelvis_y, pelvis_z])
    
    # Smoothing
    if prev_pelvis is not None:
        pelvis = pelvis * 0.7 + np.array(prev_pelvis) * 0.3
        
    return pelvis

def calculate_shoulders_with_rotation(head, head_forward, shoulder_width=0.40):
    """
    Head Forward 방향에 따라 어깨를 회전
    Shoulder Y = Head Y - 0.23m (Standard offset)
    """
    head = np.array(head, dtype=float)
    hf = np.array(head_forward, dtype=float)
    
    # Zero out Y component (keep shoulders level)
    hf[1] = 0 
    
    norm = np.linalg.norm(hf)
    if norm < 1e-6:
        hf = np.array([0., 0., -1.])
    else:
        hf = hf / norm
    
    # Right Direction (정규화된 방향)
    right_dir = np.array([hf[2], 0., -hf[0]])
    right_dir_norm = np.linalg.norm(right_dir)
    
    if right_dir_norm < 1e-6:
        right_dir = np.array([1., 0., 0.])
    else:
        right_dir = right_dir / right_dir_norm
    
    # Shoulder Y: Head - 0.23 (Normalized)
    shoulder_y = head[1] - 0.23
    
    # Calculate positions (numpy array 유지)
    left_shoulder = head - right_dir * (shoulder_width / 2)
    left_shoulder[1] = shoulder_y
    
    right_shoulder = head + right_dir * (shoulder_width / 2)
    right_shoulder[1] = shoulder_y
    
    # tolist()로 반환
    return left_shoulder.tolist(), right_shoulder.tolist()

# Initialize IK globally
ik_solver = SimpleFABRIK(user_height=1.70)
global_prev_pelvis = None

# --- ENDPOINTS ---
@app.route('/calibrate/start', methods=['POST'])
def calibrate_start():
    height_estimator.start_calibration()
    return jsonify({"status": "started", "message": "Calibration started."})

@app.route('/calibrate/finish', methods=['POST'])
def calibrate_finish():
    # 1. Finish calibration
    height = height_estimator.finish_calibration()
    
    # 2. Update IK Solver Arm Lengths with new Height!
    ik_solver.update_arm_lengths(height)
    
    return jsonify({"status": "finished", "height": height, "message": f"Calibration finished. Height set to {height:.2f}m"})

@app.route('/predict', methods=['POST'])
def predict():
    global global_prev_pelvis
    start_time = time.time()
    try:
        data = request.json
        
        def get_pos(arr):
            if not arr or len(arr) < 16: return [0,0,0]
            return [arr[12], arr[13], arr[14]]

        head_data = data.get('head', [])
        raw_head = get_pos(head_data) if len(head_data) == 16 else [0, 1.7, 0]
        
        l_data = data.get('leftHand', [])
        raw_l_hand = get_pos(l_data) if l_data else [-0.2, 1.0, 0.3]
        
        r_data = data.get('rightHand', [])
        raw_r_hand = get_pos(r_data) if r_data else [0.2, 1.0, 0.3]
        
        head_forward = data.get('headForward', [0, 0, -1]) 

        # --- 0. NORMALIZATION ---
        height_estimator.update(raw_head)
        scale = height_estimator.get_scale_factor()
        
        # Normalize Inputs
        head = np.array(raw_head) * scale
        left_hand = np.array(raw_l_hand) * scale
        right_hand = np.array(raw_r_hand) * scale
        
        # --- 1. Advanced Pelvis ---
        pelvis = estimate_pelvis_com_v2(head, left_hand, right_hand, global_prev_pelvis)
        global_prev_pelvis = pelvis.tolist()
        
        # --- 2. Lower Body Heuristic ---
        legs = {
            "leftFoot": [pelvis[0] - 0.15, 0.0, pelvis[2]],
            "rightFoot": [pelvis[0] + 0.15, 0.0, pelvis[2]],
            "leftKnee": [pelvis[0] - 0.15, pelvis[1]*0.5, pelvis[2] + 0.1],
            "rightKnee": [pelvis[0] + 0.15, pelvis[1]*0.5, pelvis[2] + 0.1]
        }
        
        # --- 3. Upper Body IK (Rotation Aware) ---
        # Calculate Shoulder positions based on Head Rotation
        left_shoulder, right_shoulder = calculate_shoulders_with_rotation(head, head_forward)
        
        # Solve Elbows
        left_elbow = ik_solver.solve_arm(left_shoulder, left_hand)
        right_elbow = ik_solver.solve_arm(right_shoulder, right_hand)

        process_end = time.time()
        
        # --- 4. DENORMALIZATION & OUTPUT ---
        inv_scale = 1.0 / scale
        def denorm(vec): return (np.array(vec) * inv_scale).tolist()
        
        skeleton = {
            "head": raw_head, 
            "neck": denorm([head[0], head[1] - 0.15, head[2]]),
            "spine": denorm([pelvis[0], pelvis[1] + 0.3, pelvis[2]]),
            "pelvis": denorm(pelvis),
            "leftKnee": denorm(legs["leftKnee"]),
            "rightKnee": denorm(legs["rightKnee"]),
            "leftAnkle": denorm(legs["leftFoot"]), 
            "rightAnkle": denorm(legs["rightFoot"]),
            "leftShoulder": denorm(left_shoulder),
            "rightShoulder": denorm(right_shoulder),
            "leftElbow": denorm(left_elbow),
            "rightElbow": denorm(right_elbow),
            "leftHand": raw_l_hand,
            "rightHand": raw_r_hand,
            "ml_latency_ms": (process_end - start_time) * 1000,
            "height_scale": scale,
            "is_calibrating": height_estimator.is_calibrating,
            "calibrated_height": height_estimator.estimated_height
        }
        
        return jsonify(skeleton)

    except Exception as e:
        logger.error(f"Prediction Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Run on port 5001
    app.run(host='0.0.0.0', port=5001)
