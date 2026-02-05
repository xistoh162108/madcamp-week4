
import sys
import os
import json
import torch
import numpy as np
import collections
import logging
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp

logger = logging.getLogger("AvatarJLM_Inference")

# Add AvatarJLM to sys.path
avatar_jlm_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'AvatarJLM'))
if avatar_jlm_path not in sys.path:
    sys.path.append(avatar_jlm_path)

# Import define_G safely
try:
    from models.select_model import define_G
except ImportError as e:
    logger.error(f"Failed to import AvatarJLM models. Check sys.path: {sys.path}. Error: {e}")
    define_G = None

class CausalResampler:
    """
    Resamples incoming 90Hz (variable) tracking data to a fixed 60Hz grid.
    Strictly Casual: Does not extrapolate. Waits for future sample to interpolate.
    """
    def __init__(self, target_fps=60.0):
        self.target_dt = 1.0 / target_fps
        self.buffer = collections.deque(maxlen=20) # Buffer for incoming 90Hz frames
        self.next_emit_time = None
        self.last_emitted_frame = None 
        
    def push(self, packet):
        """
        packet: dict containing 'timestamp' (float seconds) and transform matrices.
        """
        # Ensure packet has timestamp
        if 'timestamp' not in packet:
            packet['timestamp'] = 0.0 # Fallback
            
        self.buffer.append(packet)
        
        # Initialize time reference on first packet
        if self.next_emit_time is None:
            self.next_emit_time = packet['timestamp']

    def pop_frames(self):
        """
        Yields interpolated frames if available.
        """
        if not self.buffer:
            return

        while True:
            # Check if we have enough data to cover [next_emit_time]
            # We need t_A <= t_target <= t_B
            # buffer[0] is oldest.
            
            # Prune old packets that are no longer useful (t_B < target)
            # But keep one previous packet as t_A
            while len(self.buffer) >= 2 and self.buffer[1]['timestamp'] < self.next_emit_time:
                self.buffer.popleft()
                
            if len(self.buffer) < 2:
                # Not enough data (need at least one before and one after/at target)
                break
                
            t_data_A = self.buffer[0]
            t_data_B = self.buffer[1]
            
            t_A = t_data_A['timestamp']
            t_B = t_data_B['timestamp']
            target = self.next_emit_time
            
            if target > t_B:
                # Wait for newer data
                break
                
            if target < t_A:
                # Should not happen if we prune correctly, but if it does:
                # It means we fell behind or time reset. Reset target.
                self.next_emit_time = t_A
                continue
                
            # Perform Interpolation
            alpha = (target - t_A) / (t_B - t_A) if (t_B - t_A) > 1e-6 else 0.0
            interpolated_frame = self._interpolate(t_data_A, t_data_B, alpha)
            interpolated_frame['timestamp'] = target # assign exact target time
            
            self.next_emit_time += self.target_dt
            self.last_emitted_frame = interpolated_frame
            yield interpolated_frame

    def _interpolate(self, frame_a, frame_b, alpha):
        result = {}
        # Interpolate Head, LHand, RHand
        keys = ['head', 'leftHand', 'rightHand']
        
        for key in keys:
            mat_a = np.array(frame_a.get(key, np.eye(4)))
            mat_b = np.array(frame_b.get(key, np.eye(4)))
            
            pos_a = mat_a[:3, 3]
            pos_b = mat_b[:3, 3]
            pos_interp = pos_a * (1 - alpha) + pos_b * alpha
            
            rot_a = R.from_matrix(mat_a[:3, :3])
            rot_b = R.from_matrix(mat_b[:3, :3])
            
            # Slerp
            times = [0, 1]
            key_rots = R.from_matrix([mat_a[:3, :3], mat_b[:3, :3]])
            slerper = Slerp(times, key_rots)
            rot_interp = slerper([alpha]).as_matrix()[0]
            
            mat_out = np.eye(4)
            mat_out[:3, :3] = rot_interp
            mat_out[:3, 3] = pos_interp
            
            result[key] = mat_out
            
        return result

class FeatureExtractor:
    """
    Converts 60Hz coordinate-space frames into 396-dim Sparse Input Vector for AvatarJLM.
    Features: [Rotation(6), AngVel(6), Pos(3), LinVel(3)] * 22 Joints
    Active Joints: Head(15), LHand(20), RHand(21)
    """
    def __init__(self):
        self.prev_frame_data = None # Storage for velocity calc
        # Joint Indices
        self.IDX_HEAD = 15
        self.IDX_LHAND = 20
        self.IDX_RHAND = 21
        self.TOTAL_JOINTS = 22
        
    def extract(self, current_frame):
        """
        current_frame: dict with 'head', 'leftHand', 'rightHand' (4x4 numpy matrices)
        Returns: (396,) numpy array
        """
        feature_vec = np.zeros(396, dtype=np.float32)
        
        # Mapping from input key to joint index
        key_map = {
            'head': self.IDX_HEAD,
            'leftHand': self.IDX_LHAND,
            'rightHand': self.IDX_RHAND
        }
        
        current_struct = {}
        
        for key, j_idx in key_map.items():
            mat = current_frame[key]
            
            # 1. Rotation (6D) - Cols 0, 1
            # Mat is already Orthonormalized from Resampler (Slerp)
            rot_mat = mat[:3, :3]
            
            # Flatten columns 0 and 1
            # Note: Numpy flatten is Row-Major by default. col0 is mat[:, 0]. 
            col0 = rot_mat[:, 0]
            col1 = rot_mat[:, 1]
            # Verify Axis Mismatch Rule #6: Never flip axes manually here. T_calib handles it.
            
            rot_6d = np.concatenate([col0, col1])
            
            # Store in feature vector
            # Start index: j_idx * 6
            start_rot = j_idx * 6
            feature_vec[start_rot : start_rot+6] = rot_6d
            
            # 2. Position (3D)
            pos = mat[:3, 3]
            # Start index: 264 + j*3
            # 22*6(Rot) + 22*6(AngVel) = 264
            start_pos = 264 + j_idx * 3
            feature_vec[start_pos : start_pos+3] = pos
            
            # Store for velocity calculation
            current_struct[j_idx] = {'R': rot_mat, 'P': pos}

        # 3. Velocities
        if self.prev_frame_data is not None:
            for j_idx, curr in current_struct.items():
                if j_idx not in self.prev_frame_data: continue
                prev = self.prev_frame_data[j_idx]
                
                # Linear Velocity: P_t - P_{t-1}
                lin_vel = curr['P'] - prev['P']
                start_lin_vel = 330 + j_idx * 3
                feature_vec[start_lin_vel : start_lin_vel+3] = lin_vel
                
                # Angular Velocity: R_{rel} = R_{t-1}^T * R_t  (using Transpose as Inverse for Rotation)
                # Then take 6D of R_{rel}
                r_rel = np.matmul(prev['R'].T, curr['R'])
                
                col0_rel = r_rel[:, 0]
                col1_rel = r_rel[:, 1]
                ang_vel_6d = np.concatenate([col0_rel, col1_rel])
                
                start_ang_vel = 132 + j_idx * 6
                feature_vec[start_ang_vel : start_ang_vel+6] = ang_vel_6d
        else:
            # Cold Start: Velocity is Zero (Rule #3)
            # Already zeros initiated
            pass
            
        # Update History
        self.prev_frame_data = current_struct
        
        return feature_vec

class AvatarJLMInference:
    def __init__(self, opt_path=None, model_path=None):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.buffer = collections.deque(maxlen=41) # Window Size 41
        
        # Load Options
        if opt_path is None:
            opt_path = os.path.join(avatar_jlm_path, 'options', 'opt_ajlm.json')
            
        try:
            with open(opt_path, 'r') as f:
                self.opt = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load options from {opt_path}: {e}")
            raise e

        # Fix Support Dir Path
        # Assume support_data is in AvatarJLM root if relative
        support_dir = self.opt.get('support_dir', 'support_data/')
        if not os.path.isabs(support_dir):
            self.opt['support_dir'] = os.path.join(avatar_jlm_path, support_dir)
            
        # Initialize Model
        if define_G is not None:
            logger.info(f"Initializing AvatarJLM Model (Target: {self.opt['model']})...")
            # Hack: opt needs 'is_train' key sometimes
            self.opt['is_train'] = False
            self.netG = define_G(self.opt)
            self.netG.to(self.device)
            self.netG.eval()
            
            # Load Weights
            if model_path is None:
                # Try to find a default or explicit path
                # For now, default to placeholder or error
                # User mentioned "models/pretrained_netG" in opt? It was null.
                model_path = os.path.join(avatar_jlm_path, 'models', 'pretrained', 'avatar_jlm.pth')
            
            if os.path.exists(model_path):
                logger.info(f"Loading weights from {model_path}")
                checkpoint = torch.load(model_path, map_location=self.device)
                # Load state dict (handle potential wrapper keys)
                if 'G' in checkpoint:
                     self.netG.load_state_dict(checkpoint['G']) # If full checkpoint
                else:
                     self.netG.load_state_dict(checkpoint)
            else:
                logger.warning(f"Model weights NOT FOUND at {model_path}. Inference will be garbage.")
        else:
            self.netG = None
            
    def predict(self, feature_vec):
        """
        feature_vec: (396,) float32 vector (Block Layout)
        Returns: Skeleton Dict or None if buffering
        """
        # Add to buffer
        self.buffer.append(feature_vec)
        
        if len(self.buffer) < 41:
            return None
        
        # Prepare Input
        # Stack to (41, 396)
        input_wins = np.array(self.buffer)
        
        # Reshape to (1, 41, 22, 18)
        # Input has block layout: R(132), W(132), P(66), V(66)
        
        # Vectorized Reshape
        # Split blocks
        rot = input_wins[:, 0:132].reshape(41, 22, 6)
        ang_vel = input_wins[:, 132:264].reshape(41, 22, 6)
        pos = input_wins[:, 264:330].reshape(41, 22, 3)
        lin_vel = input_wins[:, 330:396].reshape(41, 22, 3)
        
        # Concat along feature dim (axis 2)
        # (41, 22, 6+6+3+3) = (41, 22, 18)
        input_tensor_np = np.concatenate([rot, ang_vel, pos, lin_vel], axis=2)
        
        # Add Batch Dim -> (1, 41, 22, 18)
        input_tensor = torch.tensor(input_tensor_np, dtype=torch.float32).unsqueeze(0).to(self.device)
        
        with torch.no_grad():
            if self.netG is None:
                return None
                
            # Forward Pass
            # Expects (B, T, 22, 18)
            outputs = self.netG(input_tensor)
            
            # Outputs is a dict of lists (from AvatarJLM.forward)
            # We want the LAST step output
            # 'pred_global_position' contains the Head-Aligned FK result
            pred_global_pos = outputs['pred_global_position'][-1] # Shape (B, T, 22, 3) -> (1, 41, 22, 3)
            
            # Extract latest frame (t=40)
            skeleton_pos = pred_global_pos[0, -1, :, :].cpu().numpy() # (22, 3)
            
            result = {
                "pelvis": skeleton_pos[0].tolist(),
                "head": skeleton_pos[15].tolist(),
                "leftHand": skeleton_pos[20].tolist(),
                "rightHand": skeleton_pos[21].tolist(),
                # Fill others as needed or return full list
                "full_skeleton_22": skeleton_pos.tolist()
            }
            
            return result
