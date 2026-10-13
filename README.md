# EthoRecorder2

Records up to seven Basler GigE cameras (a2A1920-51gcPRO) and one audio input (Zoom recorder used as a
USB sound card) as synchronised files, with a live view of everything while
you record.

- All cameras expose **at the same instant** (hardware-timed over the network, no trigger cable).
- Frame *N* of every camera file belongs together; the sound starts exactly at frame 0.
- Live view of all cameras; any camera can be enlarged or shown full screen, also while recording.
- Runs on Ubuntu and on Windows 10/11 from the same code.

Contents: [Wiring](#1-wiring) · [Installation](#2-installation-once-per-pc) ·
[Switching on](#3-switching-on--the-order-matters) · [Recording](#4-recording) ·
[Output files](#5-output-files) · [Frame rate and resolution](#6-frame-rate-and-resolution) ·
[How the synchronisation works](#7-how-the-synchronisation-works) ·
[Checking the sound alignment](#8-checking-the-sound-alignment-clap-test) ·
[Troubleshooting](#9-troubleshooting) · [What was tested](#10-what-was-tested)

---

## 1. Wiring

```
 Camera 1 ──network cable──┐
 Camera 2 ──network cable──┤
    ...                    ├── Netgear GS110TP (PoE ports 1-8) ──network cable── PC (gigabit port)
 Camera 7 ──network cable──┘

 Lab microphone ──► Zoom recorder ──USB cable──► PC
```

- Plug the cameras into the **PoE ports (1-8)** of the switch; they get power and data over the one cable.
- Use Cat 5e or Cat 6 cables. The PC port must be **gigabit** (a USB-3 gigabit adapter works; plug it straight into the PC, not through a USB-2 hub).
- All cameras must be on the **same switch**: that is what lets them synchronise their clocks.
- The app uses up to 7 cameras. If more are on the network it takes the 7 with the lowest serial numbers and says so under *Messages*.
- Each camera needs a lens (C-mount). Leaving the switch powered permanently does not harm the cameras.

## 2. Installation (once per PC)

Copy the whole `EthoRecorder2` folder to the PC, then:

### Ubuntu (20.04 or newer)

```bash
cd EthoRecorder2
bash install_ubuntu.sh                  # creates .venv and installs everything
sudo setup/ubuntu_network_tuning.sh     # recommended: larger network receive buffer
```

The pylon Software Suite does not have to be installed for the app itself
(the camera driver comes with it), but keep it: pylon Viewer and pylon IP
Configurator are useful for checking a camera.

### Windows 10 / 11

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/> and tick *Add python.exe to PATH*.
2. Install the **Basler pylon Software Suite** with the *GigE* option. The app runs without it, but its GigE *performance driver* prevents lost frames at full data rate.
3. Double-click **`install_windows.bat`**.
4. The first time you press *Connect*, Windows Firewall asks about Python: **allow it on private networks**, otherwise no images arrive.
5. Network adapter settings for the port the switch is on (Device Manager → adapter → *Advanced*): set *Receive Buffers* to the maximum and *Interrupt Moderation* to enabled.
6. Sound: *Settings → System → Sound → (the Zoom) → Properties*: set the format to 2 channels, 16 bit, 48000 Hz (or 44100 Hz, matching the Zoom).

### Camera network addresses

The cameras and the PC port need addresses in the same range. The simplest
arrangement, and the one this setup uses now, is automatic addresses on both
sides ("link-local", 169.254.x.x): leave the cameras on *DHCP / Auto IP* and
set the PC port to *Link-Local Only* (Ubuntu: Settings → Network → wired →
IPv4) or leave it on automatic (Windows). If the app finds no cameras, open
**pylon IP Configurator**: it shows the cameras even when the addresses do
not match and lets you fix them.

## 3. Switching on — the order matters

| Step | What | How | Wait for |
|---|---|---|---|
| 1 | **Switch** | Plug in the Netgear GS110TP power supply. | Power LED steady (about 1 min). |
| 2 | **Cameras** | Plug the camera cables into PoE ports 1-8 (if not already). | Port *PoE* LED on; camera LED green (about 20 s). |
| 3 | **PC network** | Connect the PC to the switch. | Link LED on the PC port. Allow 30 s more for the addresses. |
| 4 | **Zoom recorder** | Switch it on, connect USB, and put it in **audio interface** mode: `MENU → USB → AUDIO I/F`, choose the sampling rate (**48 kHz** recommended), `CONNECT`. Select the input the lab microphone is on and set the recording level so loud sounds stay below 0 dB. | The Zoom display shows it is connected as an interface. |
| 5 | **EthoRecorder2** | Ubuntu: `./run_ubuntu.sh` · Windows: double-click `run_windows.bat`. | Window opens. |
| 6 | **Connect** | Press **Connect**. | Images appear. The first time after power-up this takes about 30 s: the cameras are synchronising their clocks. |

Notes:

- **Close pylon Viewer first.** A camera can only be used by one program at a time.
- The Zoom must be in audio interface mode **before** you press Connect. If you plugged it in later, press *Rescan* and choose it under *Audio input*.
- The exact menu wording differs between Zoom models (H4, H4n, H5, H6 ...); look for "Audio Interface" in the USB menu.
- The sampling rate is whatever the Zoom is set to. The app measures the real rate and uses it (you get a message if it differs from 48 kHz).
- Switching off: stop the recording, wait for "Saved ...", press *Disconnect* or close the window, then power down in any order.

## 4. Recording

The panel on the right is numbered in the order you use it.

1. **Devices** – *Connect*. Check that it says `Synchronisation: PTP - simultaneous exposure`, and that the audio meter at the bottom moves when there is sound.
2. **Image size and frame rate** – choose a resolution and press *Apply image settings*. With *Highest possible* ticked the app uses the fastest rate the network allows for that size and tells you what it is (see [section 6](#6-frame-rate-and-resolution)).
3. **Brightness** – automatic by default. For behaviour recordings it is usually better to untick *Automatic* for exposure and gain once the picture looks right, so brightness cannot change during a trial. Keep the exposure short enough to freeze movement (for example 5 ms) and add light rather than gain: gain adds noise, and noisy video makes much larger files.
4. **Recording** – choose the folder and a name, then **Start recording**. Each recording goes into its own folder `name_date_time`. Press **Stop recording** and wait until it says *Saved*; finishing the files takes a few seconds for short recordings and up to a few minutes for an hour.

### Viewing one camera large / full screen

| Action | How |
|---|---|
| Enlarge one camera | Double-click its image, or choose it under *View*, or press its number (**1** ... **7**) |
| Back to all cameras | Double-click again, or press **0** |
| Full screen on / off | **F11** (or **F**), or the *Full screen* button; **Esc** leaves full screen |

The enlarged view is drawn at full sensor resolution, so it is the one to use
for focusing. All of this works during a recording and does not affect what
is saved.

### While recording, watch for

- `REC` timer and frame counter running.
- **Missing frames** (shown in red): images that did not arrive. The file still stays in step, because the gap is filled by repeating the previous frame and the frame is marked in the CSV file.
- The **CLIP** light on the audio meter: the sound is too loud; turn the Zoom's recording level down.
- Free disk space. Recording stops by itself when less than 2 GB is left.

## 5. Output files

For a recording named `trial1` started on 9 Oct 2026 at 14:30:05:

```
trial1_20261009_143005/
  trial1_20261009_143005_cam1.mp4          video, camera 1 (H.264) + sound track
  trial1_20261009_143005_cam2.mp4          video, camera 2 (H.264) + sound track
  trial1_20261009_143005_audio.wav         sound, aligned to the video
  trial1_20261009_143005_audio_raw.wav     sound exactly as captured (backup)
  trial1_20261009_143005_cam1_frames.csv   one line per frame: timestamps, status
  trial1_20261009_143005_cam2_frames.csv
  ...                                      the same two files for every further camera (cam3 ... cam7)
  trial1_20261009_143005_session.json      settings, frame rate, sync report
```

- The cameras are numbered by serial number: camera 1 has the lowest.
- **Videos**: constant frame rate, same number of frames in every file. Frame *N* in every file was exposed at the same instant.
- **`_audio.wav`**: sample 0 is the moment of frame 0, and the sound card's clock error has been corrected, so time *t* in the WAV is time *t* in the videos for the whole recording. This is the file to analyse.
- **`_audio_raw.wav`**: untouched capture, starting about 0.3 s before frame 0 (the exact position is in the JSON file). Kept as a backup; can be switched off (`keep_raw_audio`).
- **`_frames.csv`**: `status` is `ok`, or `filled` for a missing frame that was replaced by a repeat. `camera_timestamp_ns` is the camera's own exposure time stamp.
- **`_session.json`**: exact frame rate (`frame_period_us`), camera settings, missing frames, and the measured synchronisation quality.
- If the PC crashes or loses power during a recording, the videos up to that moment are still playable (they are then named `...camN.rec.mp4`, without sound track), and `_audio_raw.wav` holds the sound.
- WAV files are limited to 4 GB: about 6 hours of stereo sound at 48 kHz. Split longer sessions.

File sizes depend strongly on the scene. Measured with the default quality
(*High*): about 1-2.5 MB per second and camera in a well-lit room (measured with
two a2A2048 cameras), so roughly 5-10 GB per hour and camera; a dark, noisy picture can be several
times larger.

## 6. Frame rate and resolution

All cameras share one gigabit cable between the switch and the PC, and that
cable is the limit: it carries about 100 MB of image data per second, divided
equally between the connected cameras. A full-sensor image of the
a2A1920-51gcPRO (1920 × 1200) is 2.3 MB, so the more cameras, the lower the
frame rate:

| Resolution | 2 cameras | 4 cameras | 7 cameras |
|---|---|---|---|
| 1920 × 1200 (full sensor) | 22 fps | 11 fps | 6.4 fps |
| 1920 × 1080 | 25 fps | 12 fps | 7.1 fps |
| 1600 × 1200 | 27 fps | 13 fps | 7.6 fps |
| 1440 × 1080 | 33 fps | 16 fps | 9.4 fps |
| 1280 × 960 | 42 fps | 21 fps | 12 fps |
| 1280 × 720 | 56 fps | 28 fps | 16 fps |
| 1024 × 768 | 65 fps | 33 fps | 19 fps |
| 800 × 600 | 107 fps | 53 fps | 31 fps |

These are calculated from the data rate measured with the earlier two-camera
setup, not measured with seven cameras. The app shows the exact maximum after
*Apply image settings*.

- A smaller resolution is a **crop from the centre of the sensor**: the pixels stay as sharp, the field of view gets smaller. Use a wider lens or move the camera back to compensate.
- A long exposure time also limits the frame rate (exposure cannot be longer than one frame).
- Greyscale does not increase the frame rate: the cameras send one byte per pixel either way.

**To get the full sensor at the cameras' full 51 fps**, the cameras need a
faster path to the PC: a switch with a 10 Gbit uplink port plus a matching
network card in the PC (seven full-rate streams are about 820 MB per second).
The GS110TP's ports are all 1 Gbit. If you change the hardware, raise
`link_budget_mbytes` in the settings file accordingly; it is the total for
all cameras together.

### Advanced settings

A few rarely needed settings are only in the settings file, which is written
when the program closes:

- Ubuntu: `~/.config/EthoRecorder2/settings.json`
- Windows: `%APPDATA%\EthoRecorder2\settings.json`

| Setting | Meaning |
|---|---|
| `audio_latency_ms` | Sound alignment correction, see [section 8](#8-checking-the-sound-alignment-clap-test). |
| `gain_auto_max_db` | Upper limit for automatic gain (default 12 dB). |
| `exposure_auto_max_ms` | Upper limit for automatic exposure (default 30 ms). |
| `keep_raw_audio` | Keep `_audio_raw.wav` (default yes). |
| `x264_preset` | Encoder speed; `veryfast` by default. `faster` or `medium` give smaller files but need more CPU. |
| `link_budget_mbytes` | Total data rate of all cameras together in MB/s (default 122 for one gigabit link). |
| `packet_size` | Network packet size (default 1500). Set 8192 only if jumbo frames are enabled on the PC port **and** in the switch; gains about 4 % frame rate. |

## 7. How the synchronisation works

**Camera to camera.** The cameras support the Precision Time Protocol (PTP,
IEEE 1588). When you press *Connect* they agree on a common clock over the
network: one becomes "master", the others "slaves" that follow it to well under
a microsecond. Each camera then takes a picture every time that shared clock
passes a multiple of the frame period. Nothing is triggered from the PC, so
the PC's timing does not matter. Measured with two cameras: clock offset 0.1-0.7
µs, and time stamps of matching frames within 0.02 µs of each other.

If PTP cannot lock (the cameras are not on the same switch, for example) the
app says so and falls back to *free-run*: frames are still matched by time,
but exposures can be up to half a frame apart. The same happens with a
single camera, where there is nothing to synchronise.

**Video files.** Every frame carries its camera time stamp. The app converts
it to a position on the frame grid and writes exactly one frame per position.
A frame that did not arrive is replaced by a repeat and logged, so the files
never drift apart.

**Sound to video.** The sound card and the cameras have independent clocks,
so this is done by time-stamping: the app tracks how the camera clock and
the sound card's sample counter each relate to the PC clock, cuts the sound
to start at frame 0, and after the recording corrects the small rate
difference between the two clocks (typically 10-50 parts per million, which
would otherwise add up to 40-180 ms per hour). The rate correction is applied
to recordings longer than 30 s; shorter ones do not need it.

Expected sound-to-video accuracy is a few milliseconds, a fraction of one
video frame. The remaining uncertainty is the fixed delay inside the Zoom and
its USB connection, which the PC cannot see. If you need to know it or remove
it, do the clap test below once.

## 8. Checking the sound alignment (clap test)

1. Record 10 seconds in which you clap once, hands clearly visible to a camera, close to the microphone. A clapperboard is better. (Sound travels 34 cm per millisecond, so distance from the microphone counts.)
2. Open the `_camN.mp4` in a program that shows picture and sound wave together (for example Audacity for the WAV plus the frame CSV, or any video editor) and find the frame in which the hands meet and the position of the sound peak.
3. If the sound comes **late** by *X* ms, set `audio_latency_ms` to *X* in the settings file (negative if it comes early). The value stays valid as long as the Zoom, its settings and the PC are the same.

At 6 fps one frame lasts 160 ms, so the picture can only locate the clap to
within a frame; use the smallest resolution, which gives the highest frame
rate, for this test.

## 9. Troubleshooting

| Problem | What to do |
|---|---|
| "No Basler camera found" | Switch powered? Cable in the PC? pylon Viewer closed? Wait 30 s after plugging in. Then check the addresses with pylon IP Configurator ([section 2](#camera-network-addresses)). On Windows: allow Python in the firewall. |
| Picture is only blurry light and shadow | No lens, lens cap on, or far out of focus. Enlarge the camera, open the iris, turn the focus ring. |
| Picture too dark / too bright | Tick *Automatic* exposure and gain, or open/close the lens iris. |
| "Cameras not hardware-synchronised" | All cameras must be on the same switch. *Disconnect*, wait 10 s, *Connect*. If it persists, power-cycle the switch. |
| Missing frames, or "damaged image(s)" | Ubuntu: run `sudo setup/ubuntu_network_tuning.sh`. Windows: install the pylon GigE performance driver and raise *Receive Buffers*. Use a USB-3 port for a USB network adapter. If it continues, lower the frame rate a little (untick *Highest possible*). |
| "dropped (PC too slow)" | The video encoder cannot keep up: close other programs, choose a lower quality, or reduce frame rate or resolution. |
| No audio device in the list | Zoom not in audio interface mode, or plugged in after the program started: press *Rescan*. |
| Audio meter does not move | Wrong input selected on the Zoom, or its recording level is at zero. |
| "the device runs at 44100 Hz" | Only information: the Zoom is set to 44.1 kHz. For 48 kHz re-enter its audio interface mode and choose 48 kHz. |
| "the audio driver lost samples" | The PC was overloaded for a moment and a few milliseconds of sound are missing. Close other programs. |
| Camera temperature shown in red | The camera is too hot. Mount it on metal so it can lose heat; do not enclose it. |
| Videos very large | The picture is noisy: add light and lower the gain; or choose a lower quality. |
| Try the program without cameras | `./run_ubuntu.sh --demo` or `run_windows.bat --demo` uses two simulated cameras; `--demo 7` uses seven. |

The *Messages* box in the window shows what the program is doing and every
warning; include its text when asking for help.

## 10. What was tested

Tested on the lab PC (Ubuntu 22.04, pylon 26.09) with two a2A2048-37gcBAS
cameras on the GS110TP and a Zoom H4 as USB sound card:

- recording at 2048 × 1536, 1440 × 1080, 1024 × 768 and 800 × 600, 35 s each: no missing frames;
- a 5-minute full-sensor recording with sound: 4895 frames per camera, none missing, no lost sound samples; the sound card's clock was measured 8 ppm off the cameras' and corrected;
- camera-to-camera timing from the cameras' own time stamps (section 7);
- the complete program: connect, live view, enlarge, full screen, record, stop, change resolution.

Not tested:

- **The a2A1920-51gcPRO cameras and more than two real cameras.** Support for up to seven cameras was only run with pylon's simulated cameras (`--demo 7`: live view and a recording with seven video files). Synchronisation (PTP) and the frame rates in section 6 still have to be confirmed on the real cameras.
- **Windows.** The code is written for both systems and uses nothing Ubuntu-specific, but it has not been run on a Windows PC. Expect to check the firewall and sound settings in [section 2](#windows-10--11) the first time.
- **Absolute sound-to-video delay.** It is computed from time stamps as described in section 7; it has not been confirmed with a physical event. Do the clap test once if millisecond-level alignment matters for your analysis.

## Command line

```
./run_ubuntu.sh                         start the program
./run_ubuntu.sh --demo                  two simulated cameras (--demo 7: seven)
./run_ubuntu.sh --headless --duration 60 --output /data/test
                                        record 60 s without a window, then exit
```
