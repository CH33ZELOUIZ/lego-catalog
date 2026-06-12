# LEGO Catalog

A small Dockerized Flask app for tracking LEGO sets and minifigures, with owned/wanted lists, CSV import/export, Brickset metadata enrichment, and manual review pages for uncertain minifigures.

## Features

- Track owned and wanted LEGO sets.
- Track owned and wanted minifigures.
- Import Rebrickable-owned CSV exports.
- Export a Rebrickable-style owned CSV.
- Add/remove items manually.
- Auto-add set minifigures when adding a set, when public metadata is available.
- Pull set/minifigure names, images, MSRP/current-value details, year, pieces, and related metadata from public Brickset pages where available.
- Cache remote images locally for faster repeat browsing.
- Optional review workflow for uncertain minifigure rows and local photo references.

## Safety and source notes

This app uses public web pages for metadata enrichment. Be respectful of source sites: keep personal use low-volume, do not hammer endpoints, and prefer official APIs/datasets if your use grows.

Do not publish your live database, cached personal images, local photo folders, or secret `.env` file.

## Quick start

```bash
git clone https://github.com/<your-user>/lego-catalog.git
cd lego-catalog
cp .env.example .env
# edit .env if needed
docker compose up -d --build
```

Open <http://localhost:3012>.

Data persists in `./data/lego_catalog.db`; cached images live in `./static/cache`.

## Optional local photo review

The review page can list local HEIC photo references if you mount a folder and set `LEGO_PHOTO_REVIEW_DIR`.

Example compose volume:

```yaml
volumes:
  - ./photos:/photos:ro
```

Then set:

```env
LEGO_PHOTO_REVIEW_DIR=/photos
```

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m py_compile app.py
PORT=3012 python app.py
```

## License

MIT
