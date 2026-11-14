// Every knob for the demo video. Times in ms, lengths in px of the flat 1920x1080 frame, angles in degrees.
export const FPS = 60;
export const FADE_FRAMES = 12;          // fade through black between shots (half out, half in)

// Global look
export const LOOK = {
  blur: 16,                // tilt-shift blur strength (px) of the out-of-focus copy
  bandCenter: 50,          // focus band centre, % of frame height
  bandHalf: 17,            // half-height of the fully sharp band (%) -> ~34% of the frame is sharp
  bandSoft: 12,            // soft transition from sharp to blurred (%)
  vignetteInner: 42,       // % of the radius that stays clear
  vignetteStrength: 0.85,  // corner darkness 0..1
  grain: 0.06,             // film grain opacity (0 = off)
};

// Shots: `focus` is the point of the flat frame that sits in the sharp band, `scale` the zoom, `rotX`/`rotZ`
// the tilt, `drift` the camera path along the plane over the shot (eased; the focus moves from focus-drift/2 to focus+drift/2), `zoomDrift` the
// change in scale over the shot. `liftY` nudges the whole plane up/down on screen.
// `look` overrides LOOK for one shot (e.g. raise the focus band when the subject sits at the screen's top edge).
export const SHOTS = [
  { name: "menu",    ms: 3200, focus: [1640, 250], scale: 2.0,  rotX: 47, rotZ: -21, drift: [20, 260],   zoomDrift: 0.06, liftY: -40,
    look: { bandCenter: 46 } },
  { name: "text",    ms: 3800, focus: [1450, 300], scale: 1.75, rotX: 49, rotZ: 20,  drift: [-260, 300], zoomDrift: 0.05, liftY: -20 },
  { name: "summary", ms: 3600, focus: [1640, 190], scale: 2.3,  rotX: 45, rotZ: -20, drift: [-170, 20],  zoomDrift: 0.07, liftY: -50,
    look: { bandCenter: 35, bandHalf: 15 } },
  { name: "lock",    ms: 2600, focus: [1640, 160], scale: 2.5,  rotX: 50, rotZ: 22,  drift: [150, -10],  zoomDrift: 0.05, liftY: -50,
    look: { bandCenter: 35, bandHalf: 15 } },
  { name: "end",     ms: 2600, focus: [960, 560],  scale: 1.3,  rotX: 38, rotZ: -16, drift: [-140, 0],   zoomDrift: 0.05, liftY: 0 },
];

export const POSTER = { shot: "summary", ms: 2900 };   // the frame saved as poster.png
