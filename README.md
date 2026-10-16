# Roborock Camera

Live video for camera-capable Roborock vacuums, installed through HACS next to the built-in Roborock integration. It does not replace that integration. Vacuum controls and the map stay where they are. This adds a camera entity for vacuums that report live video.

The browser talks WebRTC directly to the vacuum. Home Assistant only forwards the offer, the answer, and ICE candidates over the existing Roborock connection.

## Install

1. In HACS, open **Integrations**, then the menu, then **Custom repositories**.
2. Add `https://github.com/pickeld/roborock-camera` as an **Integration**.
3. Download **Roborock Camera** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → Roborock Camera**.
5. Enter the video pattern from the Roborock app as digits. The dots are numbered:

   ```
   1 2 3
   4 5 6
   7 8 9
   ```

   The top row, left to right, is `123`. Use 4 to 9 dots, do not repeat a dot, and include every dot the line passes through.

The pattern is stored only as the digest the vacuum requires. A blank pattern in the integration options keeps the current digest.

## Requirements

- Home Assistant 2026.10 or newer
- The built-in Roborock integration, already set up and running
- A vacuum that reports live video (`is_video_monitor_supported` or two-key real-time video)

Vacuums with only obstacle-avoidance cameras, such as the S8 Pro Ultra, do not get a camera entity. Models that require a physical confirmation on the robot may wait for that button press before the picture starts.

One preview can run at a time. Opening the camera again replaces the previous viewer.
