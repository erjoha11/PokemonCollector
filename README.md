# PokemonCollector

A personal Pokemon Trading Card Collection tracker built as a browser-based web application. Browse, search, and manage your Pokemon card collection entirely in your browser — no server required.

## Features

- 📋 **View your collection** in a responsive card grid
- ➕ **Add new cards** with details like set, rarity, type, condition, quantity, and estimated value
- 🔍 **Search** by card name
- 🎛️ **Filter** by set, type, or rarity
- 🗑️ **Remove cards** from the collection
- 💾 **Persistent storage** — collection is saved in the browser's `localStorage` so it survives page refreshes

## Getting Started

1. Clone or download this repository.
2. Open `index.html` in any modern web browser.
3. Your collection starts pre-loaded with a few sample cards. Add your own cards using the **Add Card** button.

No build step, no dependencies, no account required.

## Card Data Fields

| Field | Description |
|-------|-------------|
| Name | Card name (e.g. "Charizard") |
| Set | Expansion set (e.g. "Base Set", "Scarlet & Violet") |
| Number | Card number within the set (e.g. "4/102") |
| Rarity | Common · Uncommon · Rare · Holo Rare · Ultra Rare · Secret Rare |
| Type | Pokémon type (Fire, Water, Grass, …) |
| Condition | Mint · Near Mint · Lightly Played · Moderately Played · Heavily Played · Damaged |
| Quantity | How many copies you own |
| Value (USD) | Estimated market value per card |

## Project Structure

```
PokemonCollector/
├── index.html          # Main application page
├── style.css           # Pokemon-themed styles
├── app.js              # Collection logic (add, remove, filter, search)
├── data/
│   └── collection.json # Sample collection data (loaded on first visit)
└── README.md
```
