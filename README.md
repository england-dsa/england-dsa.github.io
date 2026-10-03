# Data portfolio (Quarto + GitHub Pages)

The site rebuilds and publishes itself every time you change a file on GitHub.
You do not need Quarto or Git installed to get started.

## Publish it (about 10 minutes)

1. On GitHub, create a new **public** repository named `YOUR-USERNAME.github.io`
   (that exact name gives you the address `https://YOUR-USERNAME.github.io`).
2. In the new repo choose **uploading an existing file**, drag in everything from
   this folder, and commit.
   - The `.github` folder is hidden on Mac (press Cmd+Shift+. in Finder to show it).
     If it does not upload, use **Add file → Create new file**, name it
     `.github/workflows/publish.yml`, and paste in that file's contents.
3. Go to **Settings → Pages** and set **Source** to **GitHub Actions**.
4. Open the **Actions** tab, select **Publish site**, and click **Run workflow**.
   When it shows a green check, your site is live.

## Make it yours

| File | What to change |
|---|---|
| `_quarto.yml` | Site title, GitHub and LinkedIn links |
| `index.qmd` | Home page headline and tagline |
| `about.qmd` | Your bio, skills, contact |
| `projects/` | One folder per project |

Search the files for `EDIT` and `YOUR-` to find every placeholder.

## Add a project

Create a new folder in `projects/` containing either:

- **Power BI**: an `index.qmd` (copy the sample). Paste your Publish to web link
  into the iframe, or add a screenshot/GIF.
- **Python**: an `index.ipynb`. Run the notebook first so its charts are saved in
  it, and keep the first cell as a **Raw** cell holding the title, description,
  date, categories, and image.

Add a `thumbnail.png` to each folder for the home page card. It will appear on the
home page automatically, newest first.

## Preview on your own computer (optional)

Install Quarto from quarto.org, then run `quarto preview` in this folder.
