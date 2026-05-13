# JobJarvis Chrome extension

One-click "Save this job to JobJarvis" button. Detects Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, BambooHR, Recruitee, iCIMS, Workday, TeamTailor, and
Jobvite URLs.

## Install (development)

1. Open Chrome → `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked**
4. Select this `extension/` folder
5. The pin button in your toolbar — click it once to set up

## First-time setup

Click the extension icon → at the bottom of the popup:

- **JobJarvis backend URL**: `http://localhost:8000` (local) or your deployed
  URL like `https://yourname.duckdns.org`
- **API token**: open JobJarvis in another tab → Dev Tools → Application →
  Local Storage → `http://localhost:3000` → copy the value of `jj_token` →
  paste it here

## Usage

Browse to any job posting on any company's career site. Click the JobJarvis
icon in your toolbar → click "↗ Save to JobJarvis". The company is added to
your index and their jobs start appearing in `/matches` within ~10 minutes.

## Icons

Provide your own `icon16.png`, `icon48.png`, `icon128.png` in this folder, or
the extension will use Chrome's default icon. Easiest: take your JobJarvis
logo, resize to 128×128 and use the same image for all three.
