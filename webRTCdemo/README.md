## HOW TO USE

### 구조

VR --(WebRTC)-- PC --(WebRTC)-- iPhone

### 사용 방법

1. PC측에서 receiver_sensor.html 실행 (http-server -S -C localhost+1.pem -K localhost+1-key.pem -p 8443)
2. iPhone-PC 연결
   1. PC쪽에서 Start 버튼 (수신대기)
   2. iPhone쪽 IMUSender에서 방 이름 맞춰서 방이름 맞추고 Connect Signaling
   3. IMUSender에서 DataChannel state: open이 되면 Start AR눌러서 실행
3. VR-PC 연결
   1. PC쪽에서 Start Broadcast
   2. VR용 폰에서 receiver_sensor.html 접속 후 방 이름 맞추고 Join Broadcast
   3. Enable Sensors
   4. Fullscreen버튼 눌러서 전체화면 전환 후 VR 장착
