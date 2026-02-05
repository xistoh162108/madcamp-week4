import socket
import json
import logging
import numpy as np
import time
from scipy.spatial.transform import Rotation as R

# Import AvatarJLM Logic
from avatar_jlm_inference import CausalResampler, FeatureExtractor, AvatarJLMInference

# Configuration
UDP_IP = "0.0.0.0"
UDP_PORT_IN = 5001
UDP_PORT_OUT = 5003  # Sending back to Visualizer
VISUALIZER_IP = "127.0.0.1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UDP_MLServer")

# --- INITIALIZATION ---
try:
    ajlm_inference = AvatarJLMInference()
    ajlm_resampler = CausalResampler(target_fps=60.0)
    ajlm_extractor = FeatureExtractor()
    pipeline_ready = (ajlm_inference.netG is not None)
    logger.info("AvatarJLM Pipeline Ready (UDP Mode)")
except Exception as e:
    logger.error(f"Pipeline Init Failed: {e}")
    pipeline_ready = False

# --- CALIBRATION LOGIC ---
class CalibrationManager:
    def __init__(self):
        self.is_calibrating = False
        self.samples = []
        self.R_calib = np.eye(3)
        self.t_calib = np.zeros(3)
        
    def start(self):
        self.is_calibrating = True
        self.samples = []
        logger.info("Calibration Started")
        
    def update(self, head_mat):
        if self.is_calibrating:
            self.samples.append(head_mat)
            
    def finish(self):
        self.is_calibrating = False
        if not self.samples: return False
        positions = [m[:3, 3] for m in self.samples]
        avg_pos = np.mean(positions, axis=0)
        forwards = [-m[:3, 2] for m in self.samples]
        avg_fwd_raw = np.mean(forwards, axis=0)
        fwd_b = np.array([avg_fwd_raw[0], 0.0, avg_fwd_raw[2]])
        norm = np.linalg.norm(fwd_b)
        if norm < 1e-3: return False
        fwd_b /= norm
        rot = R.align_vectors([[0,0,1]], [fwd_b])[0]
        self.R_calib = rot.as_matrix()
        p_rotated = self.R_calib @ avg_pos
        self.t_calib = np.array([-p_rotated[0], 0.0, -p_rotated[2]])
        logger.info(f"Calibration Finished. t_calib={self.t_calib}")
        return True

    def apply(self, mat_4x4):
        if mat_4x4 is None: return None
        mat = np.array(mat_4x4)
        mat[:3, :3] = self.R_calib @ mat[:3, :3]
        mat[:3, 3] = self.R_calib @ mat[:3, 3] + self.t_calib
        return mat

calibrator = CalibrationManager()

def extract_transform_from_matrix(input_data):
    if isinstance(input_data, dict) and 'pos' in input_data and 'rot' in input_data:
        p, q = input_data['pos'], input_data['rot']
        rot_mat = R.from_quat(q).as_matrix()
        mat = np.eye(4)
        mat[:3, :3], mat[:3, 3] = rot_mat, p
        return mat
    if isinstance(input_data, list) and len(input_data) >= 16:
         return np.array(input_data, dtype=float).reshape(4, 4).T
    if isinstance(input_data, dict) and 'joints' in input_data:
        joints = input_data['joints']
        if len(joints) > 0:
            mat = np.eye(4)
            mat[:3, 3] = joints[0]
            return mat
    return np.eye(4)

# --- MAIN LOOP ---
def main():
    sock_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_in.bind((UDP_IP, UDP_PORT_IN))
    
    sock_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    logger.info(f"UDP ML Server listening on {UDP_IP}:{UDP_PORT_IN}")
    logger.info(f"Returning results to {VISUALIZER_IP}:{UDP_PORT_OUT}")

    last_log_time = time.time()
    packet_count = 0
    first_packet = True

    while True:
        try:
            data_raw, addr = sock_in.recvfrom(65535)
            packet_count += 1
            
            if first_packet:
                logger.info(f"SUCCESS: Received first packet from {addr}!")
                first_packet = False
            
            if packet_count % 10 == 0: # Log more often for now
                logger.info(f"Packet Status: Received {packet_count} packets...")

            payload = json.loads(data_raw.decode('utf-8'))
            
            # Special Commands
            if "cmd" in payload:
                cmd = payload["cmd"]
                logger.info(f"Command received: {cmd}")
                if cmd == "calib_start": calibrator.start()
                elif cmd == "calib_finish": calibrator.finish()
                continue

            # Process Data
            input_source = payload.get('raw_udp', payload)
            
            # Timestamp Handling: VP sends ms, Resampler wants seconds.
            raw_ts = input_source.get('timestamp', None)
            if raw_ts is not None:
                # Simple heuristic: if > 1e9, it's likely ms
                if raw_ts > 1e9:
                    timestamp = raw_ts / 1000.0
                else:
                    timestamp = raw_ts
            else:
                timestamp = time.time()
            head_raw = extract_transform_from_matrix(input_source.get('head', {}))
            lhand_raw = extract_transform_from_matrix(input_source.get('leftHand', {}))
            rhand_raw = extract_transform_from_matrix(input_source.get('rightHand', {}))

            if calibrator.is_calibrating:
                calibrator.update(head_raw)
                continue

            # Apply Calibration
            head = calibrator.apply(head_raw)
            lhand = calibrator.apply(lhand_raw)
            rhand = calibrator.apply(rhand_raw)

            # Resample & Predict
            ajlm_resampler.push({'timestamp': timestamp, 'head': head, 'leftHand': lhand, 'rightHand': rhand})
            frames = list(ajlm_resampler.pop_frames())
            
            if frames:
                frame = frames[-1]
                if pipeline_ready:
                    start_t = time.time()
                    features = ajlm_extractor.extract(frame)
                    result = ajlm_inference.predict(features)
                    dur = (time.time() - start_t) * 1000
                    
                    if result:
                        # Success! Send back via UDP
                        result_bytes = json.dumps(result).encode('utf-8')
                        sock_out.sendto(result_bytes, (VISUALIZER_IP, UDP_PORT_OUT))
                        
                        if packet_count % 5 == 0:
                            logger.info(f"Inference OK ({dur:.1f}ms) - Sent to {VISUALIZER_IP}:{UDP_PORT_OUT}")
            else:
                if packet_count % 5 == 0:
                    logger.info("Waiting for more frames in resampler (Collecting...)")

        except Exception as e:
            logger.error(f"Loop Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
