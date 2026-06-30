/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // Elevated/editorial palette for the CV Structure Editor (README §"Design Tokens").
      // accent tints are precomputed static hex (Tailwind v3 can't evaluate color-mix at build).
      colors: {
        cv: {
          accent: "#3B5BD9",
          "accent-soft": "#EDF0FC", //  ~9% accent on white  (chip bg, tints)
          "accent-soft2": "#E2E6F9", // ~15% accent on white  (drag/selection halo)
          "accent-border": "#B5C1F1", // ~38% accent on white (focus/selected borders)
          canvas: "#F4F2ED",
          surface: "#FFFFFF",
          subtle: "#FAF8F4",
          sunk: "#F0EDE6",
          border: "#E7E2DA",
          border2: "#DCD6CB",
          ink: "#211C16",
          ink2: "#6B6358",
          ink3: "#A39B8D",
          danger: "#B4543E",
          "json-string": "#3E7A5E",
          "json-number": "#A8581E",
          "json-bool": "#9A4DB8",
        },
      },
      fontFamily: {
        geist: ['Geist', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        "geist-mono": ['"Geist Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
        newsreader: ['Newsreader', 'ui-serif', 'Georgia', 'serif'],
      },
      keyframes: {
        cvspin: { to: { transform: "rotate(360deg)" } },
        cvpulse: { "0%,100%": { opacity: "1" }, "50%": { opacity: "0.5" } },
        cvfade: { from: { opacity: "0", transform: "translateY(6px)" }, to: { opacity: "1", transform: "translateY(0)" } },
        cvbar: { from: { transform: "scaleX(0)" }, to: { transform: "scaleX(1)" } },
      },
      animation: {
        cvspin: "cvspin 0.7s linear infinite",
        cvpulse: "cvpulse 1.4s ease-in-out infinite",
        cvfade: "cvfade 0.16s ease-out",
      },
    },
  },
  plugins: [],
}
