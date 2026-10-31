# Science Studio

Interactive, browser-based tools for learning science by doing. Each tool is a single HTML file with no dependencies or build step: open `index.html` in any modern browser (or serve the folder with `python3 -m http.server`).

| Tool | What you can do |
|---|---|
| [`chemistry-lab/`](chemistry-lab/index.html) | Pour acids, bases, salts, indicators and metals into a beaker. A mole-based reaction engine handles neutralisation, precipitation (AgCl, BaSO₄, Cu(OH)₂), metal + acid, displacement and carbonate + acid reactions, with a live pH meter, thermometer, gas counter, ion view and a titration curve. Includes six guided experiments. |
| [`circuit-lab/`](circuit-lab/index.html) | Drag parts onto a grid: batteries, wires, resistors, bulbs, LEDs, switches, capacitors and motors. A nodal-analysis solver computes every voltage and current each frame, so bulbs glow with power, LEDs only conduct forwards (and burn out above 50 mA), capacitors charge over time and current is animated along the wires. |
| [`how-it-works/`](how-it-works/index.html) | Animated, labelled cutaways: a four-stroke engine (scrub through the cycle), a physics-driven DC motor (toggle the commutator, change the voltage) and a pin-tumbler lock (try the right key, a wrong key or a blank). The explanation panel follows the animation step by step. |

## Adding a new topic

- **Chemistry:** add a reagent to `LIQUIDS`/`SOLIDS` and a rule in `react()`; the colour, pH and readouts pick it up automatically.
- **Circuits:** add an entry to `TYPES`, a `stamp()` case (conductance `g` and source current `i`, so the part current is `g·(va − vb) + i`) and a `drawSymbol()` case.
- **Machines:** add an object with `name`, `phases`, `facts`, `draw(t)` and `explain(t)` (or `step(dt)` with `simulated: true` for physics-driven models) to `MACHINES`.
