I'm creating an audio-reactive LED strip installation that will be strapped on the outside of metal scaffolding at a small festival:
- two horizontal 30m LED lines (6 x 5m strips per line), one above the other
- total LED length = 60m x 30 LEDs/m = 1800 LEDs

I'm going to drive the LEDs data channel from a Gledopto controller (similar to but cheaper than QuinLED Dig-Quad) (with ESP32 and WLED).
The ESP32 will get its control signals from a local raspberry Pi server. The Raspberry Pi will have a basic audio microphone and a simple script to detect BPM in realtime, which is then used to modulate the LED driving signal.

I will have a single, waterproof controller box containing the Pi and audio sensor, underneath the DJ booth. That box will be about 10m away from the actual metal scaffolding where the LEDs are. Therefore, i'll mount the Gledopto directly underneath the scaffolding, connecting it to the Pi with a 10m solid ethernet cable.

## Gear-list (to buy)
-  12 x 5m strips of 12V WS2815 IP67 (waterproof and lower voltage drop) with 30 LEDs/m.
- Gledopto GL-C-618WL controller (with ESP32 and WLED) --> I will get two of these in case I mess up (backup). (This Gledopto has 4 data pins, fuses and a built-in logic level shifter.) I will get the Ethernet variant (with built in RJ45 port and PHY chip) so the control signal from the Pi can be directly plugged into the Gledopto with an ethernet cable to ensure a stable, lag-free connection.
- Raspberry Pi 4 4GB | 64GB Edition | 15W Power Supply (already have one)
- 2X Mean Well HLG-320H-12 power supply (must deal with wind and rain since it will be outside, underneath the metal scaffolding).
- an **I2S digital microphone (like the INMP441)** wired directly to the Pi's GPIO (wire length <15cm). Sealed in an IP65 box the mic can't hear anything, so the enclosure needs an **acoustic port** — either a small hole covered with a Gore-Tex / waterproof acoustic membrane, or mount the mic on the *outside* with a short cable through a sealed gland.
- 2x IP65 ABS enclosure with cable glands for waterproofing the Pi control box and Gledopto.
- cheap aluminum LED channels with milky diffusers to zip-tie onto the scaffolding, diffusing the individual LEDs. Will get mounted directly on the metal scaffolding, so will automatically be grounded.
- a cheap box of **"fork" or "ring" crimp connectors** for the stripped copper wires (the fork slides perfectly under the screw terminal of the power supply for a rock-solid connection.)
- Adhesive heat shrink / Silicone sealant (neutral cure) to waterproof all cut points.
- 2x **automotive-style fused bus bars** (e.g., a Blue Sea Systems mini add-a-circuit block, or any 6–12 way blade fuse distribution block with a common input). If any of the strips short, its 10A fuse blows silently, the rest keep running. I'll use the fuse blocks to fuse _each_ 18 AWG injection line at 5A–7.5A, rather than just heavily fusing the main bus.
- Additional ~25–30A fuse per PSU output (protects against **bus short before branch fuses**).
- A ton of **Black, UV-resistant** zip-ties.
- **Heat Gun:** For that marine-grade adhesive heat shrink.
- Cables:
	- Long (40m) 14 AWG (outdoor-rated, eg H07RN-F) cable for the mains AC power supply line to the LED PSU's
	- Long (<100m) 18 AWG cable to connect the injection taps going from the 12 AWG bus into the LED strips. 12 LED strips, roughly 6x5m + 6x10m = 90m
	- short 18 AWG two-conductor cable to connect Gledopto to the 12V PSU
	- a shielded 4-conductor alarm cable for the first data run from the ESP32 to the LEDs.
	- A short ethernet patch cable to connect the Pi to the Gledopto inside the control box.

### Tools needed:
- Tool to strip back the outer jacket of a standard 240V power cable
- Soldering gear
## Power setup:

1800 LEDs * 0.015A = **27 Amps total draw**

I will create a **power injection bus** to power the LEDs:
1. I'll run a thick, stranded copper wire (12 AWG) parallel to the entire 30m run.
2. I will center-feed the power bus at the 15m mark to minimize voltage drop along both sides and run the **+12V AND GND as a twisted pair** to reduce noise coupling into data. PSU 1 powers half of the LEDs, and PSU 2 power the other half. They will share a common ground wire between their negative terminals, but their V+ injection buses must never touch.
3. Inject power at the junction of every two 5m strips (the 5m, 15m, and 25m marks). By injecting at the joint, I'm simultaneously powering the end of one 5m strip and the beginning of the next. With this scheme strips 1 (0–5m) and 6 (25–30m) only get fed at *one* end, so the LEDs at 0m and 30m sit a full 5m of trace away from injection — fine on WS2815, but during burn-in I'll watch for visible drop at those endpoints and add taps at 0m / 30m if needed. At every junction, tap into that thick wire and inject 12V and Ground into the two neighbouring LED strips.
4. The _Data_ line (and the Backup Data line) will flow continuously from the start to the end of the 30m run, but the _Power_ is distributed evenly alongside it.

I will run a set of small-gauge wires (e.g., 18 AWG) from one of the Mean Well PSUs simply to power the Gledopto's `V+` and `GND` input terminals. The LED data lines run from the Gledopto's 4 output terminals to the strips.
**Crucial:** The Gledopto’s `GND` output terminal must be connected to the common ground bus shared by the PSUs and the LEDs. Do not connect the Gledopto's `V+` output terminals to the LED strips at all. All LED power comes straight from the external fuse blocks.

Since the WS2815 LEDs have high idle power draw, its a good idea to turn off the main 40m AC line power source at night.
## Software
The control interface will run on the raspberry through a server that exposes a public (password protected) url on the local network so the core team can tweak settings on their phone. The Raspberry will then generate the control signals for the LEDs (optionally including realtime audioreactivity modulation) and send those to the ESP32.

I will use DDP (Distributed Display Protocol). WLED (on Gledopto) supports DDP natively. It is much more efficient than E1.31 or Art-Net because it doesn't have the "universe" limitations and uses less header data.
1. The Python script on the Pi processes the audio. 
2. It calculates the RGB values for all 1800 LEDs. 
3. It wraps those values in a DDP packet. 
4. It sends that packet over WiFi (or Ethernet) to the Gledopto’s IP address.

The Gledopto has 4 data outputs. Use Output 1 for the top 30m row (900 LEDs) and Output 2 for the bottom 30m row (900 LEDs). WLED maps this seamlessly, and the framerate will jump to a buttery smooth 40+ FPS.

There will be a local WiFi router on-site so the Gledopto and Pi will receive local IP addresses. For maximum data quality, I will connect the Gledopto and Pi with an Ethernet cable and configure the python server to send data over that address. I'll configure a **Static IP** on the Raspberry Pi's `eth0` interface (e.g., `10.0.0.1`) and pre-configure the WLED Ethernet settings on the Gledopto to a static IP on the same subnet (e.g., `10.0.0.2`).

For real-time BPM detection from a microphone:
**First approach:**  **LedFx**. It is an open-source, audio-reactive software built specifically for this. It runs on a Raspberry Pi, takes in audio from an ALSA device (like the INMP441), calculates the math in real-time, and spits out DDP directly to WLED.

Alternatively, 'm thinking to use **`aubio`** — it has a Python binding (`pip install aubio`) with a dedicated `tempo` object designed for exactly this: continuous onset detection and BPM estimation from a live audio stream via `pyaudio` or `sounddevice`. Main reason is that I can go full custom on the signal processing side and vibecode a really cool, customizable audio interface.

The Pi will expose a web interface so the team can change effects from their phones on the local network. Since the Pi might often get randomly shut down when the main power source is turned off at night, it should be configured to deal with this gracefully:
Set the Raspberry Pi OS to run in **Read-Only mode**. Since the OS never writes to the SD card, there is zero risk of corruption during a power loss:
- **How:** Go to `sudo raspi-config` > **Performance Options** > **Overlay File System** and enable it.
- **Note:** Your Python script won't be able to save logs or local files unless you mount a specific "writeable" partition or send data to an external cloud database.


## Pro Setup Tips:

#### The "Backup Data" Line (WS2815 Speciality)

The WS2815 has four pads: 12V, Ground, Data (DI), and Backup Data (BI).

- **Critical:** With the 4-output center-feed there are **four chain starts** (one per Gledopto output, at the 15m mark on each row). On *each* of those four starting strips, the BI (Backup In) wire must be tied to **Ground**. If BI is left floating at the start of a chain, it can pick up interference and cause flickering. After the first pixel of each chain, BI simply connects to the DI of the previous pixel as intended.
- **Strip direction at the centre:** the strips running 15m → 0m and 15m → 30m on each row need to be physically mounted with their data-arrow pointing *away* from the centre (i.e. two of the six strips per row are flipped relative to "natural" left-to-right reading). Plan and label this before mounting.

### The Framerate Bottleneck (Data Center-Feeding)

**The Issue:** Pushing 900 LEDs down a single data pin takes time. The WS281X protocol runs at a fixed 800kHz. 900 LEDs will cap framerate at roughly **~33 FPS**. For fast, audio-reactive EDM visuals, 33 FPS can feel slightly sluggish or stuttery. **The Fix:** I'm already center-feeding power at the 15m mark --> center-feed the **data** there too!

- Mount the Gledopto in the exact center of the scaffolding (the 15m mark).
- Use all 4 data outputs of the Gledopto
- Output 1: Top Left (450 LEDs)
- Output 2: Top Right (450 LEDs)
- Output 3: Bottom Left (450 LEDs)
- Output 4: Bottom Right (450 LEDs)
- **Result:** drops the data payload to 450 pixels per pin, doubling the refresh rate to a buttery smooth **~66 FPS**, with zero extra hardware.

### Tie both PSU grounds together.
Run a heavy ground wire between the negative terminals of both HLG PSUs. Without a common ground reference, the data signal between rows can behave unpredictably, and I may also get ground loops causing flickering.

### Gledopto Boot
When you boot up the Gledopto and access the WLED interface, you will need to go to `Config -> LED Preferences` and manually assign those specific GPIOs to your four 450-LED segments. You will also need to go to `WiFi Setup -> Ethernet Type` and select `Gledopto Series with Ethernet` (or the generic LAN8720 setting with the correct clock pins) to ensure the physical RJ45 port wakes up.

### Data Signal Integrity

The ESP32 sends a 3.3V signal. The Gledopto has a shifter to bring it to 5V. However, over a 30m run, the data signal is typically "regenerated" by every single LED.

- **The Danger Zone:** The distance between the **Gledopto and the first LED**. If that cable is longer than 3–5 meters, I might get flickering. Use a shielded cable for that first data run if it's long.
- (Option 1) **Sacrificial Pixels**: If the control box is more than 5 meters from the start of the scaffolding, cut a single WS2815 LED off a strip, place it halfway between the box and the scaffolding, wrap it in heat shrink, and use it as a "sacrificial pixel." It will receive the data signal and freshly repeat it at full strength to the main strips.
- (Option 2) **Most solid solution:** The Gledopto has an Ethernet port. So I can just keep the Raspberry Pi and its microphone at the DJ booth. Put the Gledopto in a small, waterproof enclosure directly **on the metal scaffolding** right next to the LED strips. Run a standard, rugged 10m Ethernet cable from the Pi to the Gledopto. Ethernet is balanced, differential, and designed to carry data flawlessly over 100 meters in high-noise environments. This completely eliminates the data-run danger zone.

Another good idea is twisting the **Data wire with a Ground wire**. Inside the 4-conductor alarm cable from the Gledopto to the strips, use one twisted pair for Data + Ground, and another for Backup Data + Ground.

### Waterproofing the Connections:
The WS2815 strips are IP67 (in a silicone sleeve), but the moment I cut them or plug them into each other to inject power, I break that seal. I will use **marine-grade, adhesive-lined heat shrink tubing** over every injection joint and connection to keep the rain out.

To make a continuous 30m run that can survive wind and rain, I will cut the plastic connectors off entirely. Pull back the silicone sleeve about an inch, solder the pads directly to each other (or use short, thick bridging wires), solder the power injection wires to those same joints, inject neutral-cure silicone into the tube opening, and slide the marine-grade adhesive heat shrink over the whole joint.

The Mean Well HLG-320H-12 is rain-resistant, but not meant to sit in puddles: should be mounted underneath the scaffold with the terminals facing down (so water doesn't pool on the housing). I will add **drip loops** on all cables entering boxes.

Since the Pi 4 will be processing continuous live audio and sealed inside an IP65 waterproof enclosure under the DJ booth, I might need some kind of passive heat sink here, TBD.