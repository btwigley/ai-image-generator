# AI Image Generator — Quick Start

## Web App (Recommended)

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

1. Click **Settings** (top right) and enter your OpenAI API key
2. Select a character from the sidebar (or click **+ New**)
3. Fill in identity traits — core description auto-generates
4. Upload **reference images** (3-8) for identity consistency
5. Add variation rows (scene + outfit required, rest optional)
6. Go to **Generate** tab, review cost estimate, click **Generate Images**
7. Browse results in the **Gallery** tab

## CLI (Advanced)

The CLI tool works for scripted/batch workflows:

```bash
# Preview prompts
python batch_image_generator.py parallel -c characters/example_character.json -v characters/example_variations.csv --preview 3

# Generate (parallel, real-time)
python batch_image_generator.py parallel -c characters/example_character.json -v characters/example_variations.csv -y

# Generate (batch, 50% cheaper via OpenAI Batch API)
python batch_image_generator.py batch -c characters/example_character.json -v characters/example_variations.csv -y

# Check batch status
python batch_image_generator.py batch-status -c characters/example_character.json -v characters/example_variations.csv
```

## Reference Images

When reference images are uploaded for a character, generation automatically:
- Uses the OpenAI Image **Edit** API instead of Generations
- Enables **high input fidelity** for better identity preservation
- Appends identity-lock instructions to each prompt

For best results, upload 3-8 reference images showing the character from different angles, expressions, and lighting conditions.
