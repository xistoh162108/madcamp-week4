# WebRTC Skeleton Protocol Specification (V1.2)

## Overview
This document defines the 124-byte binary protocol used for streaming Apple Vision Pro (AVP) tracking data over WebRTC. The protocol is optimized for real-time avatar control by providing absolute world-space positions and direction vectors for the head, limbs, and the lightsaber (iPhone).

## Packet Structure
- **Total Size**: 124 Bytes
- **Endianness**: Little-Endian (LE) for all fields
- **Data Types**: All 3D coordinates and vectors are `Float32` (4 bytes each).

| Offset | Field | Type | Description |
| :--- | :--- | :--- | :--- |
| **0** | `header` | `u32` | Bit 0-30: Sequence Number, **Bit 31: Stale Flag** (1=Safe Pose) |
| **4** | `head_pos_x` | `f32` | Head Position X (Meters) |
| **8** | `head_pos_y` | `f32` | Head Position Y |
| **12** | `head_pos_z` | `f32` | Head Position Z |
| **16** | `head_fwd_x` | `f32` | Head Forward Vector X (Normalized) |
| **20** | `head_fwd_y` | `f32` | Head Forward Vector Y |
| **24** | `head_fwd_z` | `f32` | Head Forward Vector Z |
| **28** | `l_palm_pos_x` | `f32` | Left Palm Position X |
| **32** | `l_palm_pos_y` | `f32` | Left Palm Position Y |
| **36** | `l_palm_pos_z` | `f32` | Left Palm Position Z |
| **40** | `l_palm_fwd_x` | `f32` | Left Palm Direction X (Pointing forward) |
| **44** | `l_palm_fwd_y` | `f32` | Left Palm Direction Y |
| **48** | `l_palm_fwd_z` | `f32` | Left Palm Direction Z |
| **52** | `l_elbow_pos_x` | `f32` | Left Elbow Position X (**Tracked Joint 26**) |
| **56** | `l_elbow_pos_y` | `f32` | Left Elbow Position Y |
| **60** | `l_elbow_pos_z` | `f32` | Left Elbow Position Z |
| **64** | `r_palm_pos_x` | `f32` | Right Palm Position X |
| **68** | `r_palm_pos_y` | `f32` | Right Palm Position Y |
| **72** | `r_palm_pos_z` | `f32` | Right Palm Position Z |
| **76** | `r_palm_fwd_x` | `f32` | Right Palm Direction X |
| **80** | `r_palm_fwd_y` | `f32` | Right Palm Direction Y |
| **84** | `r_palm_fwd_z` | `f32` | Right Palm Direction Z |
| **88** | `r_elbow_pos_x` | `f32` | Right Elbow Position X (**Tracked Joint 26**) |
| **92** | `r_elbow_pos_y` | `f32` | Right Elbow Position Y |
| **96** | `r_elbow_pos_z` | `f32` | Right Elbow Position Z |
| **100** | `sword_pos_x` | `f32` | Sword (iPhone) Position X (Meters) |
| **104** | `sword_pos_y` | `f32` | Sword (iPhone) Position Y |
| **108** | `sword_pos_z` | `f32` | Sword (iPhone) Position Z |
| **112** | `sword_fwd_x` | `f32` | Sword Forward Vector X (Hilt-to-Tip) |
| **116** | `sword_fwd_y` | `f32` | Sword Forward Vector Y |
| **120** | `sword_fwd_z` | `f32` | Sword Forward Vector Z |

## Coordination System
- **Unit**: Meters (m)
- **Axis**: Right-Handed Coordinate System (X: Right, Y: Up, Z: Back)
- **Forward Reference**: 
  - Head Forward: Refers to `-Z` in local head space.
  - Palm Forward: Refers to `+Z` in local palm space (pointing away from wrist).

## Stale Flag Logic
When the **31st bit** of the header is set (`header & 0x80000000`), the data is "Stale". In this state:
- The server sends a **Base Pose** (T-Pose).
- Remote peer should transition smoothly to this pose or maintain the last valid pose if preferred.

## Parsing Example (Python)
```python
import struct

def parse_100b_packet(data):
    # Header
    header = struct.unpack('<I', data[0:4])[0]
    is_stale = bool(header & 0x80000000)
    seq = header & 0x7FFFFFFF
    
    # 30 floats starting from index 4
    floats = struct.unpack('<30f', data[4:124])
    
    return {
        "seq": seq,
        "is_stale": is_stale,
        "head": {"pos": floats[0:3], "fwd": floats[3:6]},
        "left": {"palm": floats[6:9], "fwd": floats[9:12], "elbow": floats[12:15]},
        "right": {"palm": floats[15:18], "fwd": floats[18:21], "elbow": floats[21:24]},
        "sword": {"pos": floats[24:27], "fwd": floats[27:30]}
    }
```
