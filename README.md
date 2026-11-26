# FrostWatch API: deploy guide

## 1. Put the code on GitHub
Create a free GitHub account, make a new repository called `frostwatch-api`,
and upload the three files in this folder (`main.py`, `requirements.txt`,
`README.md`).

## 2. Edit one line
In `main.py`, change `USER_AGENT` to include your real email. The National
Weather Service requires this.

## 3. Deploy on Render
1. Sign up at render.com with your GitHub account.
2. New, then Web Service, then pick your `frostwatch-api` repository.
3. Settings:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Instance type: Free is fine for testing (it sleeps after idle, so the
     first request after a quiet spell takes about 30 seconds)
4. Click Deploy. When it finishes, Render shows your address, for example
   `https://frostwatch-api.onrender.com`.

## 3b. Check that it works
Open this in your browser (swap in your address):

    https://frostwatch-api.onrender.com/frost-risk?zip=30301

You should see JSON with three nights of frost outlook.

## 4. Connect Wix
Open `frostwatch-embed.html`, find the line that says `API_URL`, and paste
your Render address there. Then paste the whole file into a Wix Embed HTML
element.

## Customizing the forecast
All the frost logic lives in `score_night()` in `main.py`. Change the
thresholds, add cloud cover, wind, or your own model output there.
