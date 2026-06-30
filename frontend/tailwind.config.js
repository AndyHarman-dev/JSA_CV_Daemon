/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // Cyberpunk daemon redesign: chrome colors live in src/theme/tokens.ts (Tailwind v3
      // can't evaluate color-mix() at build time, and the design's accent-derived fills
      // need it). Tailwind here only carries fonts; layout/spacing utilities stay plain.
      fontFamily: {
        "chakra-petch": ['"Chakra Petch"', "system-ui", "sans-serif"],
        "ibm-plex": ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        "share-tech-mono": ['"Share Tech Mono"', "ui-monospace", "monospace"],
        newsreader: ["Newsreader", "ui-serif", "Georgia", "serif"],
      },
    },
  },
  plugins: [],
}
