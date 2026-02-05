
import logging
from flask import Flask, request, jsonify
import numpy as np
import time
from collections import deque
from scipy.spatial.transform import Rotation as R

# Import AvatarJLM Logic
from avatar_jlm_inference import CausalResampler, FeatureExtractor, AvatarJLMInference

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MLServer")

# --- GLOBAL STATE ---
# Pipeline Components
try:
    ajlm_inference = AvatarJLMInference()
    ajlm_resampler = CausalResampler(target_fps=60.0)
    ajlm_extractor = FeatureExtractor()
    pipeline_ready = (ajlm_inference.netG is not None)
except Exception as e:
    logger.error(f"Pipeline Init Failed: {e}")
    pipeline_ready = False
    ajlm_inference = None

last_valid_skeleton = None

class CalibrationManager:
    def __init__(self):
        self.is_calibrating = False
        self.samples = []
        # T_calib: Transforms ARKit World -> Model World
        # Default Identity
        self.R_calib = np.eye(3)
        self.t_calib = np.zeros(3)
        
    def start(self):
        self.is_calibrating = True
        self.samples = []
        logger.info("Calibration Started (Target: Yaw-Only, Floor=0)")
        
    def update(self, head_mat):
        if self.is_calibrating:
            self.samples.append(head_mat)
            
    def finish(self):
        self.is_calibrating = False
        if not self.samples:
            return False
            
        # Compute Average Head Matrix (Position & Forward)
        # Naive average of positions
        positions = [m[:3, 3] for m in self.samples]
        avg_pos = np.mean(positions, axis=0)
        
        # Robust Forward Vector
        # Extract forward (-Z) from each sample, average them
        forwards = [-m[:3, 2] for m in self.samples]
        avg_fwd_raw = np.mean(forwards, axis=0)
        
        # Project to XZ plane (y=0)
        fwd_b = np.array([avg_fwd_raw[0], 0.0, avg_fwd_raw[2]])
        norm = np.linalg.norm(fwd_b)
        if norm < 1e-3:
            logger.warning("Calibration Failed: User looking straight up/down?")
            return False
        fwd_b /= norm
        
        # Compute Yaw Rotation to align fwd_b -> +Z (0,0,1)
        # R_calib * fwd_b = (0,0,1)
        # Using scipy: from_to_rotation
        # Vector A: fwd_b, Vector B: [0,0,1]
        rot = R.align_vectors([[0,0,1]], [fwd_b])[0]
        self.R_calib = rot.as_matrix()
        
        # Translation: Move P_user to (0, P_user_y, 0).
        # We want T_calib * P_user_world ~= (0, P_user_y, 0)
        # (R_calib * P_user) + t_calib = (0, P_user_y, 0)
        # t_calib = (0, P_user_y, 0) - (R_calib * P_user)
        # STRICT RULE: t_calib.y MUST be 0.
        # So we only compensate X and Z.
        
        p_rotated = self.R_calib @ avg_pos
        # We want p_final.x = 0, p_final.z = 0.
        # p_final.x = p_rotated.x + t_calib.x = 0 => t_calib.x = -p_rotated.x
        # p_final.z = p_rotated.z + t_calib.z = 0 => t_calib.z = -p_rotated.z
        
        self.t_calib = np.array([-p_rotated[0], 0.0, -p_rotated[2]])
        
        logger.info(f"Calibration Finished. t_calib={self.t_calib}")
        return True

    def apply(self, mat_4x4):
        # Apply T_calib to 4x4 matrix
        # P_new = R_c * P_old + t_c
        # R_new = R_c * R_old
        if mat_4x4 is None: return None
        mat = np.array(mat_4x4)
        
        # Rot
        mat[:3, :3] = self.R_calib @ mat[:3, :3]
        
        # Pos
        mat[:3, 3] = self.R_calib @ mat[:3, 3] + self.t_calib
        
        return mat

calibrator = CalibrationManager()

def extract_transform_from_matrix(input_data):
    # Case 1: JSON Object with {pos: [x,y,z], rot: [x,y,z,w]}
    if isinstance(input_data, dict) and 'pos' in input_data and 'rot' in input_data:
        p = input_data['pos']
        q = input_data['rot']
        # Construct Matrix
        rot_mat = R.from_quat(q).as_matrix()
        mat = np.eye(4)
        mat[:3, :3] = rot_mat
        mat[:3, 3] = p
        return mat

    # Case 2: List of 16 floats (Column-Major)
    if isinstance(input_data, list) and len(input_data) >= 16:
         return np.array(input_data, dtype=float).reshape(4, 4).T # Col-Major to Row-Major Numpy
         
    # Case 3: Skeleton Dict {joints: [[x,y,z], ...], ...}
    if isinstance(input_data, dict) and 'joints' in input_data:
        joints = input_data['joints']
        if len(joints) > 0:
            pos = joints[0]
            mat = np.eye(4)
            mat[:3, 3] = pos
            return mat

    # Fallback / Identity
    return np.eye(4)
    
@app.route('/calibrate/start', methods=['POST'])
def calibrate_start():
    calibrator.start()
    return jsonify({"status": "started"})

@app.route('/calibrate/finish', methods=['POST'])
def calibrate_finish():
    success = calibrator.finish()
    return jsonify({"status": "finished" if success else "failed"})

@app.route('/predict', methods=['POST'])
def predict():
    global last_valid_skeleton
    start_time = time.time()
    try:
        data = request.json
        input_source = data.get('raw_udp', data)
        
        # 1. Parse Input
        timestamp = input_source.get('timestamp', time.time())
        head_raw = extract_transform_from_matrix(input_source.get('head', []))
        lhand_raw = extract_transform_from_matrix(input_source.get('leftHand', []))
        rhand_raw = extract_transform_from_matrix(input_source.get('rightHand', []))
        
        # Scale Check: ARKit/Unity usually meters.
        
        # 2. Update Calibration
        if calibrator.is_calibrating:
            calibrator.update(head_raw)
            # Return dummy or uncalibrated
            return jsonify({"status": "calibrating"})
            
        # 3. Apply Calibration (ARKit -> Model World)
        head = calibrator.apply(head_raw)
        lhand = calibrator.apply(lhand_raw)
        rhand = calibrator.apply(rhand_raw)
        
        # 4. Push to Resampler
        packet = {
            'timestamp': timestamp,
            'head': head,
            'leftHand': lhand,
            'rightHand': rhand
        }
        ajlm_resampler.push(packet)
        
        # 5. Process Resampled Frames
        frames = list(ajlm_resampler.pop_frames())
        
        if not frames:
             return jsonify({"status": "buffering"})

        # Only process the LATEST frame for real-time responsiveness
        # (Preprocessing multiple frames would cause tail latency buildup)
        frame = frames[-1]
        
        result = None
        if pipeline_ready:
            # Feature Extract (Sparse 396)
            features = ajlm_extractor.extract(frame)
            # Inference
            output = ajlm_inference.predict(features)
            if output:
                result = output
        
        # Return prediction result
        if result:
            last_valid_skeleton = result
            last_valid_skeleton['ml_latency_ms'] = (time.time() - start_time) * 1000
            if calibrator.R_calib is not None:
                last_valid_skeleton['is_calibrated'] = True
            
            # Log every few frames
            if int(time.time() * 2) % 2 == 0:
                logger.info(f"Inference OK: Latency: {last_valid_skeleton['ml_latency_ms']:.1f}ms")
                
            return jsonify(last_valid_skeleton)
        
        return jsonify({"status": "processing"})

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
