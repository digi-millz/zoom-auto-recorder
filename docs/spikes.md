# Spike results (measured, not required)

These are dated observations from **one** machine, not a specification: the box's current details live in
`MACHINE.md` (gitignored, replaced per machine). They stay in the repo because they are the evidence behind the
code's choices — window classes, capture method, audio shape — and because the numbers are reproducible by re-running
the same probes. Hardware changes are recorded in place; see the 2026-11-15 section at the end.

At the 2026-11-14 measurement: Windows 11 26300, Zoom desktop (per-user install), OBS Studio 33.0.0,
obs-websocket 5.7.6, Python 3.13.2, 96 DPI / 100 % scaling.

## Zoom windows observed

| Class | Meaning |
| --- | --- |
| `ZPPTMainFrmWndClassEx` | Zoom Workplace home window (not a meeting) |
| `ConfMultiTabContentWndClass` | in-meeting window (Zoom Workplace 7.x) — title is the meeting topic |
| `ZPMeetingMainWndClass` / `ZPContentViewWndClass` | older meeting frame classes |
| `zWaitingRoomWndClass` | waiting room |
| `zRichMsgBoxClass` | disclosure / consent / confirmation dialog |
| `VideoPreviewWndClass` | pre-join preview with the Join button |
| `ZPToolBarParentWndClass` | floating meeting toolbar (no client size; not capturable) |
| `zoom_acc_notify_wnd`, `zoom_pt_*` | hidden notification helpers (no title) |

## Capture: which method actually renders Zoom

`PrintWindow(PW_RENDERFULLCONTENT)` — works, non-black, correct even when covered. This is what ``win_capture``
uses for reading dialogs.

OBS `window_capture` against a live Zoom window (luma max; 0 = black frame):

| `window` value | priority | method 3 (WGC) | 2 (DXGI) | 1 (BitBlt) | 0 (auto) |
| --- | --- | --- | --- | --- | --- |
| `"Zoom Webinar"` (title only) | 0 | 0 | 0 | 0 | 0 |
| `"ConfMultiTabContentWndClass"` (class only) | 1 | 0 | 0 | 0 | 0 |
| `"Zoom.exe"` (exe only) | 2 | 0 | 0 | 0 | 0 |
| `"Zoom Webinar:ConfMultiTabContentWndClass:Zoom.exe"` | 0 | **0** | **255** | **255** | **255** |

Conclusions:

1. The full `"title:class:exe"` composite is **required**; partial matches never resolve.
2. **WGC cannot capture Zoom's CEF windows** — OBS's default "automatic" resolves to DXGI and works today, but the
   method is pinned to 2 so a future default cannot silently land on the one that fails.
3. DXGI and BitBlt returned identical content with another window raised over Zoom, so the capture stays on the
   target window rather than the monitor. (Frozen-frame behaviour while fully covered is still unverified; the
   watcher keeps the meeting window in front anyway.)

## OCR

`Windows.Media.Ocr` (en-US) works: 88 words off the Zoom home window, accurate boxes. Real UI text reads cleanly
("Home / Calendar / Chat / Canvas / Scheduler / Settings / Search"), long labels occasionally split
("The 9 30"), and proper nouns garble ("Jesse ogers") — which is why the matcher tolerates a 0.85 fuzzy ratio and
matches whole lines rather than requiring exact strings.

## OBS scene shape (verified against a live meeting, OBS 33.0.1)

* window capture: `window_capture`, `window="<title>:<class>:Zoom.exe"`, `priority=0`, `method=2`,
  `capture_mode="window"`, `cursor=False`
* audio: `wasapi_process_output_capture` (Application Audio Capture) on the same composite → Zoom's output only
* output: this install defaulted to **Hybrid MP4**, which is **not** crash-safe in practice: force-killing OBS mid
  recording left a **0-byte** file (the footer never got written). The recorder therefore forces **mkv**
  (`ensure_recording_format`), which keeps what was written, and `postprocess.finalize` remuxes to mp4.

* encoders present: NVENC H.264/HEVC, QSV H.264/HEVC, x264 (GTX 1060 + UHD 630). Recordings land at 1280x720 / 30 fps
  because that is the active OBS profile's canvas, not the window size (1550x830).

### OBS crash behaviour (measured)

| Event | Result |
| --- | --- |
| `Stop-Process obs64` during an mp4/hybrid_mp4 recording | 0-byte file, unusable |
| same during an mkv recording | playable part (12.6 s / 9.1 MB in the test), remuxed to mp4 |
| crash detection | `GetRecordStatus` fails → `EXIT_LOST` (socket `ConnectionResetError 10054`), not `EXIT_RECORD` |
| recovery | OBS relaunched, scene re-prepared, new part started; test finished with part 1 + part 2, exit 0 |

Two traps found here: `find_obs_exe` could not see a winget install (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\…`),
and a starting OBS (process up, socket not yet listening) was fatal instead of waited for.

### End-to-end result (live webinar, 5.1 s)

`scene.prepare` → `start_record` (confirmed active) → `stop_record` (path returned) → `postprocess.finalize`:

```
FINAL 2026-11-14_18-20_zoom-webinar.mp4 dur=5.1s size=3.93MB video=True audio=True remuxed=False
audio: mean_volume -26.2 dB, max_volume -4.9 dB   # real signal, not silence
```

OBS wrote `simple_file_output` with `simple_aac` (160 kb/s stereo) + NVENC.

## OBS crash/safe-mode trap

OBS keeps an unclean-shutdown marker at `%APPDATA%\obs-studio\.sentinel` (a **directory**). While it exists, the
next launch shows a modal **"OBS Studio Crash Detected — run in safe mode?"** and **the WebSocket server never
starts**, i.e. the recorder silently fails while nobody is at the screen. A force-kill (`Stop-Process -Force`)
guarantees it.

`obs_controller.clear_crash_flag()` deletes it before launch (refusing while an `obs64.exe` is alive). Verified:
after clearing, the WebSocket is up in 2 s and no dialog appears.

## Dialog inventory (measured across four live joins)

| Window class | Text seen | Action |
| --- | --- | --- |
| `zWaitingMeetingIDWndClass` | "Enter your email and name" | click the field placeholder, type the email, then **Join meeting** |
| `zSimpleMessageDialogClass` | "Join new meeting? … leave this meeting" | **Leave & join** (only rule that leaves a call) |
| `zRichMsgBoxClass` — "Disclosure" | "By clicking \"Agree\" and continuing…" | **Agree** — and the quoted word in the paragraph must lose to the button |
| `VideoPreviewWndClass` | meeting topic + camera preview | **Join** |
| `zWaitingRoomWndClass` | "Waiting for the host to start the meeting" | nothing (a meeting state, not a dialog) |
| `ZDisclaimerBannerWndClass` | "This meeting is being recorded." | nothing (banner, no button) |
| `zLeaveWndClass` | "Leave webinar" / "Cancel" | nothing automatic (guessing between Leave and End meeting is unsafe) |

Dialog-sized OCR is fine (560x426 and 700x533 captures produced exact boxes); the quoted-label trap above was the
only matching failure, fixed by ranking candidates on line word-count and vertical position.

## Audio: the echo (measured)

A recording sounded doubled. Cause: OBS mixes its **global** `Desktop Audio` device (whole system, which already
contains Zoom) into every scene *in addition to* the per-process `Zoom audio` source. `scene.mute_global_audio()`
mutes `Desktop Audio` and `Mic/Aux` on every run.

Proof: playing a 440 Hz system tone and recording 5 s gave `mean_volume: -91.0 dB` (digital silence) with the
globals muted, versus audible capture before. Zoom itself is still captured once, by the per-process source.

## End-to-end runs observed

| Link | Zoom's path | Outcome |
| --- | --- | --- |
| `…/89712840393` (already joined) | in-meeting | 40 s take, dialog watcher 1 click / 1 verified |
| `…/85330823611` | Disclosure → prejoin → waiting room | 30 s take of the waiting room, host never admitted |
| `…/89538600487` | prejoin → admitted | 21 s take of the real meeting ("Wealth Workshop") |
| scheduled via UI | join → record → stop on window close | 111 s / 82 MB, exit 0, named `…_zoom-89712840393.mp4` |

## 2026-11-15 — live re-verification (fresh checkout)

Same machine, but the hardware and the dependency set had moved: **RTX 2070 SUPER** (not the GTX 1060 above) +
UHD 630, three 1920×1080 panels, OBS 33.0.1, a new `.venv`. Four defects only a live join can expose:

| Symptom | Measurement | Cause / fix |
| --- | --- | --- |
| Watcher blind, join stuck at "Enter your email and name" | 14 × `OCR failed: No module named 'winrt'` | `requirements.txt` never listed the OCR ladder; pywinrt now pinned (`winrt-runtime`, `.Media.Ocr`, `.Graphics.Imaging`, `.Storage`, `.Globalization`, `.Foundation`) |
| Recording had valid streams but every frame black | `ffmpeg -ss 30 … -frames:v 1` → max luma **0**; `scene.source_luma` **0** | OBS was outputting the **user's** scene (`Zoom Meet`), not the app's: `scene.prepare` built `Zoom auto-recorder` but never made it live. Luma of the same source read **255** immediately after `SetCurrentProgramScene` |
| "email entry confirmed" yet the field was empty | before/after screenshots show the placeholder still there | typing 250 ms after the click lost the CEF focus, and "the text changed" was accepted as success. Now: 0.5 s settle, and the address itself must appear in the re-read |
| `could not write the run report: [WinError 3] … _shots\<id>` | a join that saw no dialog has no shots folder | `_write_report` now treats a missing folder as "no screenshots" |

Verified after the fixes, against the 16:00 link (waiting room, host absent):

```
13:56:31 OBS confirmed the recording is active
13:56:32 Zoom process exited              ← Zoom killed mid-recording
13:56:33 OBS confirmed the recording stopped
13:56:33 recording ready: …_zoom-89712840393.mp4 (12s, 9.2 MB, video=True audio=True, remuxed)
exit 0 · verify: duration 12.3s, 1 video stream, 1 audio stream, PASS · frame at t=5s: max luma 255
```

Still unproven: the `close_grace` branch (meeting window disappears while `Zoom.exe` keeps running). Closing the
CEF waiting-room window with `WM_CLOSE` did not actually close it, so the recorder correctly kept running —
`select_meeting_window` requires a visible window. The process-exit branch above covers the same code path minus
the `find_process()` check.

### 21:00 entry — the prejoin "Join" button was invisible to OCR

`ed90d84f` (Wealth Workshop Pre-Kick Off Call) ended **exit 4** after the 300 s join timeout. Zoom *had* opened the
prejoin window; the watcher never clicked Join, so no meeting window ever existed. Re-running the matcher offline on
that run's own screenshot:

| Input | OCR | `dialogs.pick` |
| --- | --- | --- |
| 646×524 capture, as-is | 17 words, **no "Join"** ("Backgrounds", "Always show this preview when joining" both read) | `None` |
| same frame at 2× (Lanczos) | 18 words, `Join` at `(547,474,27,10)` | `Click(rule='prejoin', label='Join', x=560, y=479)` |

White-on-blue button labels below ~16 px are dropped by `Windows.Media.Ocr`; that is why `ocr.words` now upscales
frames under 1000 px by 2 and divides the boxes back. Same run, two more measured defects: the unchanged prejoin
window re-alerted every ~7 s (**346 PNGs, 18 MB**), and the 16:00 recording — 11.6 GB / 4 h 12 m — lost its remux to
a fixed 600 s ffmpeg timeout, which left an **11 GB mp4 with no moov atom** ("Invalid data") beside the good mkv.

Fixes: alerts are remembered per window signature; the remux timeout scales with file size (1449 s for that file),
`+faststart` is skipped above 4 GB, and a failed remux deletes its partial output.

### 2026-11-16 09:00 — the prejoin email never landed

`da6a1c0d` ended **exit 4** after 300 s. Zoom's "Enter your email and name" prompt was on screen; the log repeated
`email entry not confirmed in zWaitingMeetingIDWndClass; will retry once`, and then the watcher clicked "Join meeting"
four times with `click NOT verified` — the button is disabled while the field is empty, so nothing ever moved.

Three causes, in order of weight:

1. **`_fill_prejoin` never raised the window.** The dismiss path calls `focus(hwnd)` before clicking because a
   synthetic click lands on whatever is on top; the email path did not, so the click hit another window, the CEF field
   never took focus, and the keystrokes went nowhere. Fixed: focus, settle 150 ms, click, settle 500 ms, type, and the
   address itself must appear in the re-read.
2. **Two live watchers.** At 11:39 screenshots were written to both `_shots/20261115-210000` and
   `_shots/20261116-090000`, and OCR logged `Another RecognizeAsync operation is already running!` — two sessions
   were polling at once, stealing focus and clicks from each other. Two runners had been started while the lock file
   was being deleted by hand during diagnosis. `RunnerLock.acquire` now re-reads the file and only counts the lock as
   held if the pid on disk is its own.
3. **The process predated the OCR fix.** The runner had been started at 13:57 the day before, so it was still running
   the pre-upscale `ocr.words` (and the old remux timeout).
