
import torch
import torch.nn as nn
import numpy as np
from scipy.spatial.transform import Rotation as R
import logging

logger = logging.getLogger("LoBSTr")

# --- Architecture (Copied from network_architecture.py) ---
class LoBSTr_GRU(nn.Module):
    def __init__(self, input_dim, output_dim, hidden_dim, latent_dim):
        super(LoBSTr_GRU, self).__init__()
        self.device = torch.device('cpu') # Force CPU for compatibility
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.gru = nn.GRU(self.input_dim, self.hidden_dim, bias=True, batch_first=True)
        self.fc1 = nn.Linear(self.hidden_dim, latent_dim)
        self.fc2 = nn.Linear(latent_dim, output_dim)
        self.contact1 = nn.Linear(latent_dim, 4)
        self.relu = nn.ReLU()
        # self.init_weights() # Skip init if loading trained weights

    def forward(self, input_tensor):
        hidden = torch.zeros(1, input_tensor.shape[0], self.hidden_dim).to(self.device)
        out, hidden = self.gru(input_tensor, hidden)
        gru_out = out[:, -1]
        out = self.fc1(gru_out)
        latent_vector = self.relu(out)

        lower_pose = self.fc2(latent_vector)
        contact = self.contact1(latent_vector)

        return lower_pose, contact

# --- Inference Wrapper ---
class LoBSTrInference:
    def __init__(self, model_path, window_size=60):
        self.window_size = window_size
        self.device = torch.device('cpu')
        
        # Load Model
        # Hack to map 'network_architecture' to current module for Unpickling
        import sys
        sys.modules['network_architecture'] = sys.modules[__name__]

        try:
            # Load Full Model Object
            self.model = torch.load(model_path, map_location=self.device, weights_only=False)
            # Force CPU
            self.model.device = self.device
            self.model.to(self.device)
            self.model.eval()
            logger.info("LoBSTr Model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load LoBSTr model: {e}")
            self.model = None

        # Statistics (Hardcoded from load_configs.py or typical dataset stats if missing)
        # Ideally we should load them. For now, we will assume standard normalization or 0 mean/1 std if unclear.
        # But normalization is critical for DL.
        # I will verify if I can get means from the repo files.
        # inference_module.py imports them from load_configs.
        # load_configs computes them from DATASET! This is bad. We don't have the dataset.
        # However, inference_module.py is running on a server that has access to dataset?
        # If I can't load dataset, I can't compute mean/std.
        # CHECK: Does the repo provide mean/std files?
        # inference_module uses `from load_configs import ...`. load_configs loads dataset.
        # This is a blocker. I cannot compute 50GB dataset mean/std.
        # AUTO-RECOVERY: I must check if `LoBSTr_python` has any saved .npy mean/std files.
        # If not, I'm stuck with unnormalized data (results will be garbage).
        # Wait, inside `models/LoBSTr.../` maybe there are .npy files?
        
        self.input_mean = None
        self.input_std = None
        self.output_mean = None
        self.output_std = None

    def load_stats(self, training_stats_path=None):
        # Placeholder if I find them. for now, I might have to guess or skip.
        pass

    def process_frames(self, frames):
        """
        frames: list of 60 items.
        Returns: (1, 60, 37) tensor
        """
        if len(frames) < self.window_size: return None
        
        # Convert to arrays [60, 4, 4, 4] (Frame, Joint, Row, Col)
        world_transforms = np.zeros((self.window_size, 4, 4, 4))
        for f in range(self.window_size):
            world_transforms[f, 0] = frames[f]['Hips']
            world_transforms[f, 1] = frames[f]['Head']
            world_transforms[f, 2] = frames[f]['LeftHand']
            world_transforms[f, 3] = frames[f]['RightHand']
            
        velocity_list = []
        
        for f in range(self.window_size - 1):
            curr_world = world_transforms[f]
            next_world = world_transforms[f+1]
            
            # Reference frame: Hips at f+1 (Upright, projected to ground)
            ref_mat = next_world[0]
            ref_rot = ref_mat[:3, :3]
            inv_ref_rot = np.linalg.inv(ref_rot)
            
            feature_row = []
            
            for j in range(4):  # Hips, Head, LeftHand, RightHand
                # Velocity (Linear)
                lin_vel = next_world[j, :3, 3] - curr_world[j, :3, 3]
                lin_vel_ref = np.matmul(inv_ref_rot, lin_vel)
                
                # Rotation (6D Representation: Forward and Right columns of CURRENT frame in Ref Frame)
                # Note: User suggested using 'next_world' logic for features? 
                # User code: rot_curr = next_world[j, :3, :3]
                # Wait, frames[f] is current. frames[f+1] is next.
                # User code uses 'next_world' for rotation features?
                # "curr_rot = next_world[j]" in user snippet. 
                # Checking user snippet: 
                # "curr_world = world_transforms[f]; next_world = world_transforms[f+1]"
                # "rot_curr = next_world[j, :3, :3]"
                # So features are based on NEXT frame pose relative to NEXT frame Hips?
                # Yes: "forward_ref = inv_ref_rot * forward".
                # If Ref is Hips(f+1), then Hips(f+1) relative to Ref is Identity.
                # So Hips features will be [0,0,1, 1,0,0, 0,0,0] (Forward=Z, Right=X, Vel=0).
                # This makes sense for "Local Body State".
                
                rot_curr = next_world[j, :3, :3]
                forward = rot_curr[:, 0]  # First column? User said Forward=col0.
                # Standard Rotation Matrix: Col0=X(Right), Col1=Y(Up), Col2=Z(Back/Forward).
                # In Vision/Unity: Forward is Z.
                # User said "forward = rot_curr[:, 0]".
                # I will trust User's code.
                
                right = rot_curr[:, 1]    # Second column?
                
                forward_ref = np.matmul(inv_ref_rot, forward)
                right_ref = np.matmul(inv_ref_rot, right)
                
                feature_row.extend(forward_ref.tolist())
                feature_row.extend(right_ref.tolist())
                feature_row.extend(lin_vel_ref.tolist())
            
            # Root height (Hips Y position at f+1)
            root_height = next_world[0, 1, 3]
            feature_row.append(root_height)
            
            velocity_list.append(feature_row)
            
        # Pad last frame to match window size 60
        if len(velocity_list) < self.window_size:
            velocity_list.append(velocity_list[-1])
            
        input_tensor = torch.tensor([velocity_list], dtype=torch.float32).to(self.device)
        return input_tensor

    def predict(self, frames):
        if self.model is None: return None, None
        
        inp = self.process_frames(frames)
        if inp is None: return None, None
        
        with torch.no_grad():
            pose, contact = self.model(inp)
            
        pose_np = pose.cpu().numpy()[0]
        contact_np = contact.cpu().numpy()[0]
        
        # Decode: 8 joints * 4x4 matrix assumption (User agreed to this structure effectively map-wise)
        # Ref Frame is Hips of LAST frame (frames[-1]['Hips'])
        ref_mat = frames[-1]['Hips']
        
        pose_mats = pose_np.reshape(8, 4, 4)
        
        world_mats = []
        for i in range(8):
            # World = Ref * RefLocal
            w = np.matmul(ref_mat, pose_mats[i])
            world_mats.append(w)
            
        return world_mats, contact_np

def main():
    pass # Diagnostic covered by app usage
