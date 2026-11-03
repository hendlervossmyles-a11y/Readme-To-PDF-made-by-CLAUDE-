# readme_to_pdf

A small Python tool that converts a repository's `README.md` into a PDF, including its screenshots, tables, code blocks, and a clickable table of contents.

## Credit and Disclosure

**This tool was written entirely by Claude (Claude Sonnet 5.5, model ID `claude-sonnet-5-5`), an AI assistant made by [Anthropic](https://www.anthropic.com), in November 2026.** That includes the code, its testing, and this README. It was produced through conversation with Claude, based on my requests and bug reports.

**I am not claiming this code as my own work.** I am using it as a helpful tool, and the credit for writing it belongs to Claude and Anthropic.



> Anthropic. (2026). *Claude Sonnet 5.5* [Large language model]. https://claude.ai. Used to generate `readme_to_pdf.py`, November 2026.

## Setup

Requires Python 3.9+ and three packages:

```
pip install markdown-it-py reportlab pillow
```

## Usage

Put `readme_to_pdf.py` in your repository and run it from anywhere inside the repo:

```
python readme_to_pdf.py                      # finds README.md in the current folder or repo root
python readme_to_pdf.py path/to/README.md    # convert a specific file
python readme_to_pdf.py -o report.pdf --a4   # custom output name, A4 paper
```

The PDF is saved as `README.pdf` next to the README unless `-o` is given.

## Features

- **Screenshots:** image paths are resolved from the README's own folder, then the repo root. Relative paths (`docs/shot.png`), paths with spaces, and `<img src="..." width="400">` all work. Web images are downloaded.
- **Image grids:** images inside tables are scaled to fit their cells, so none are cropped.
- **Clickable table of contents:** links like `[Install](#install)` jump to the matching heading, and the PDF gets a bookmark sidebar.
- **Formatting:** headings, bold and italic, lists, tables, quotes, links, and wrapped code blocks.

## Limitations

- SVG images (such as badge icons) are not supported and show their alt text instead.
- Emoji are removed from the output.
- A missing image prints a warning and appears as `[image: alt text]` in the PDF.
