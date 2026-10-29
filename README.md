# Route Check

Best driving route between two saved addresses, e.g. work and home (here: the hospital and home),
with live traffic, plus a list of every accident, breakdown, lane/road closure and jam on the corridor.
Data: TomTom (routing with live traffic + traffic incident details) and 511NJ, NJDOT's official
statewide feed (crashes, disabled vehicles, lane blockages as the Turnpike Authority / NJDOT report
them; public RSS, no key). Each run can also produce a PDF report and an interactive map (HTML) in
`reports\`.

## One-time setup: the free TomTom key

1. Go to https://my.tomtom.com and register (email + password, **no credit card**). Confirm the
   verification email if one arrives.
2. Open https://my.tomtom.com/keys (**API & SDK Keys** in the left menu). A key called
   **My first API key** is already there: click it and copy the key string.
   If the page offers product checkboxes, make sure **Routing API**, **Search API** and **Traffic API**
   are ticked; if it doesn't, the key covers them already.
3. Open `.env` in this folder (Notepad is fine) and paste it after the equals sign:
   `TOMTOM_API_KEY=your-key-here`. No quotes, no spaces. Save.
   A brand-new key can take a few minutes to go live (you'd see an HTTP 403 until then).
4. In the same `.env`, add the two addresses (copy the four lines from `.env.example`):
   `HOSPITAL_LABEL`, `HOSPITAL_ADDRESS`, `HOME_LABEL`, `HOME_ADDRESS`. The address is what TomTom
   looks up; the label is what the reports and buttons show. The code itself holds no addresses.

Free monthly allowance (Oct 2026): Routing 20K, Geocoding 20K, Traffic Incident Details 2.5K.
One check spends 1 routing call + 1 incidents call, so roughly 80 checks a day stays free.
Python 3.14 with `requests`, `python-dotenv` and `reportlab` is already installed on this PC.

## Run it

- **Desktop shortcut "Route Check"** or double-click `route.bat`: pick the direction, then answer
  *Leaving now?* Press Enter for now, or type a time such as `5:30pm`, `17:30`, `tomorrow 7am`,
  `fri 6:15am`. A bare time that has already passed today is read as tonight / tomorrow.
- Straight from a shortcut or terminal (right-click a shortcut -> Properties -> add after the path):

```
route.bat                       menu; each run also writes reports\route_<dir>_<date>_<time>.pdf and .html, opens both
route.bat back                  home -> hospital (screen only)
route.bat --now --pdf           hospital -> home, leave now, no questions, with the PDF
route.bat --now --map           same, with the interactive map instead
route.bat back --at "tomorrow 7am"
route.bat --no-tolls --alts 3   avoid tolls, compare 3 alternates (0-5)
route.bat --pdf --map --no-open write the PDF and the map without opening them
route.bat --json                also saves the raw TomTom replies to cache\last_run.json
```

`python route_check.py ...` takes the same arguments. The PDF is one landscape page or two:
the route table, what is on each route, the accident list, the 511NJ official reports, and
clickable Google Maps / Waze links.

**Map** (`--map`): `reports\route_<dir>_<date>_<time>.html`, opened in your browser. The best route
is blue, alternates grey; slow stretches sit on top of the route in yellow / orange / red / purple by
severity. Circles are the TomTom accident issues from the report (big with a dark ring = on a route,
small and faded = nearby), small squares are 511NJ reports, tiny grey-blue dots are slowdowns nearby.
Hover or click anything for the details. The panel (top left; a bar at the bottom on a phone, tap
*Show*) has a big **Open in Waze** button and a smaller **Google Maps** one at the top, visible even
when the panel is folded away, and the route table: click a row to highlight that route. The layer
button (top right) picks the base map (free Esri tiles: Streets, the default; Dark, the same street
map with its colours inverted; Satellite, with road names on top; the map switches to the next one
by itself if tiles fail to load) and turns alternates, slow stretches, incidents and 511NJ reports
on and off. It needs an internet connection for the map tiles; the route and incident data are
inside the file.
Maps made before this change still use OpenStreetMap's own tile server, which now blocks them
(grey "Access blocked" squares); run the check again for a new map.

## Sharing it with another phone (Tailscale)

`serve.bat` turns this PC into a small Route Check website for your phones: a box for the
departure time, an *Avoid tolls* tick box and two big buttons, hospital → home and
home → hospital (named with the labels from `.env`). Tap one, wait half a minute, and the interactive map opens. Under each button
you see when that direction was last checked, with its Map, PDF and Text. The phones reach it
through [Tailscale](https://tailscale.com), a free private network between your own devices, at an
address like `https://<this-pc>.<tailnet>.ts.net` (serve.bat prints the real one). Only devices on
your Tailscale network can open it; it is not on the public internet.
The map it opens has **Home**, **PDF**, **Text** and **Refresh** links at the top (Refresh
checks that direction again, leaving now), so you never need the browser's Back button.

One-time setup (about 15 minutes, all done by you):

1. Make a free account at https://tailscale.com (sign in with Google, Microsoft or Apple).
2. Install Tailscale on this PC, either `winget install tailscale.tailscale` in a terminal or the
   installer from https://tailscale.com/download, and sign in with that account.
3. Install the Tailscale app on both phones. Either sign in with the same account, or invite the
   other person under **Users** in the admin console (https://login.tailscale.com/admin); the
   free plan covers up to three users.
4. In the admin console open **DNS** and turn on **HTTPS Certificates** (that is what gives the
   `https://...ts.net` address).
5. Set this PC to never sleep when plugged in (Settings -> System -> Power -> Screen and sleep).
6. Double-click `serve.bat`. Open the address it prints on each phone, then use the browser's
   **Add to Home Screen** (Safari: the Share button; Chrome: the ⋮ menu) so it opens like an app.

Start it with Windows: press **Win+R**, type `shell:startup`, Enter. Right-click-drag `serve.bat`
into that folder and choose **Create shortcuts here** (optionally: shortcut Properties -> Run:
*Minimized*). Closing the server window stops the site; `serve.bat off` stops the Tailscale
sharing too.

- Every tap is a real check, so the same ~80 checks a day of the free TomTom key apply. Two people
  tapping the same button at the same time share one check.
- Some hospital Wi-Fi blocks VPNs like Tailscale; switch the phone to mobile data there.
- Only need it at home? `python server.py --lan` (or `serve.bat --lan`) also opens it on the home
  Wi-Fi at `http://<this-pc's-ip>:8000`, no Tailscale needed. Windows Firewall asks once: allow
  *Private networks* only.
- The server keeps the newest 30 reports per direction in `reports\` and deletes older ones.
  Each visit is logged to `cache\server.log`.

## Hosting it for free on Streamlit (no PC needed)

`app.py` is the same check as a small web app for [Streamlit Community Cloud](https://streamlit.io/cloud),
which runs it for free on their servers, so the phones work even when this PC is off. Same page:
a *Leaving at* box, *Avoid tolls*, and the two direction buttons. A result shows **Open in Waze**
and **Google Maps** buttons, the interactive map, **Download PDF** and the text report. The last
result for each direction is shared, so the second person can tap **Show last result** without
spending another check.

**Public repo, private data.** Community Cloud now only deploys public apps from **public** GitHub
repos, so the code is public. It holds no addresses, no key and no password: those six settings
live only in `.env` here (git-ignored) and in the app's **Secrets** on Streamlit. Anyone could find
the app's address, so it asks for a password (`APP_PASSWORD`) before showing anything; without one
set it refuses to run. Share the password with the other person; each phone enters it once per
visit (reopening the page after it has been closed asks again).

Test it here first: put `APP_PASSWORD=...` in `.env` (or set it in the terminal), then
`streamlit run app.py`; it reads the key and the addresses from `.env`.

One-time setup (about 10 minutes, all done by you, in a terminal in this folder):

1. Replace the old private `route-check` repo on GitHub with a public one made from this folder
   (`.env`, `cache\` and `reports\` are never uploaded; `.gitignore` keeps them out, and this
   folder's git history was restarted so it holds no address either):

   ```
   gh auth refresh -h github.com -s delete_repo
   gh repo delete cwecht15/route-check --yes
   gh repo create route-check --public --source . --remote origin --push
   ```

   The first line is needed once, to allow `gh` to delete repos (a browser page asks you to
   confirm). The second deletes the old private repo, history and all. The third creates the
   public repo and pushes this folder to it.

2. Go to https://share.streamlit.io and **sign in with GitHub**. If an app from the old repo is
   still listed, delete it (⋮ -> Delete).
3. **Create app** / **New app** -> deploy from GitHub: repository `route-check`, branch `main`,
   main file path `app.py`.
4. **Advanced settings**: Python version **3.12**. In the **Secrets** box paste these six lines
   with your own values (the key and addresses as in `.env`; all in quotes):

   ```toml
   TOMTOM_API_KEY = "paste-key-here"
   APP_PASSWORD = "choose-a-password"
   HOSPITAL_LABEL = "Hospital name, Town"
   HOSPITAL_ADDRESS = "123 Example St, Town, NJ 00000"
   HOME_LABEL = "Home"
   HOME_ADDRESS = "456 Example Ave, Town, NJ 00000"
   ```

   Then **Deploy** and wait a few minutes for the first build.
5. The app lives at an address like `https://<name>.streamlit.app`. Open it on each phone, enter
   the password, and use **Add to Home Screen** (Safari: the Share button; Chrome: the ⋮ menu).
   Give the other person the address and the password.

- After 12 hours without visits the app goes to sleep; the next visit shows a "wake up" button
  and it is back in about half a minute.
- The first check after each wake-up also looks up the two addresses again (2 extra TomTom
  calls), since `cache\` is not uploaded.
- Changing the app: edit here, then `git add -A`, `git commit -m "..."` and `git push` from this
  folder; Streamlit redeploys by itself within a minute or two. Never put an address, key or
  password in a file that is not git-ignored: the repo is public.
- Changing the key, the password or an address: app Settings -> Secrets (and `.env` here).
- The Tailscale version (`serve.bat`) keeps working independently; both use the same TomTom key and
  its daily allowance.

## Reading the output

```
BEST   via GSP S                         58 min   (+9 min traffic)   51.2 mi   arrive 5:30 PM
       on this route:
         Accident   GSP S: exit 117 to exit 114     +3 min   minor   certain  3 rpt  4:10 PM
         Slow stretch  GSP S, 2.1 mi                +6 min   moderate (jam, 22 mph)
ALT 1  via NJ Tpke S, I-195 E            64 min   (+4 min traffic)   55.0 mi   arrive 5:36 PM   +6 min vs best
```

- **BEST / ALT n**: routes ranked by time with current traffic. `+N min traffic` is the delay versus
  free-flow. "via" names the roads that carry most of the distance.
- **on this route**: incidents within ~120 m of that route's line, and TomTom's slow stretches
  (a stretch can be the same jam an incident describes; both are shown).
- **ACCIDENT ISSUES**: every accident, broken-down vehicle, dangerous condition, lane closure and road
  closure in the corridor. `ON BEST` / `ON ALT n` / `nearby` says which route it touches. Columns:
  delay, severity (minor / moderate / major / closure), how sure TomTom is (certain / probable /
  risk of), number of reports, time of the last report. Anything not *certain* is tagged
  **POTENTIAL**: something that may blow up before you get there.
- **511NJ official reports**: what NJDOT and the Turnpike Authority have logged on or within a mile of
  your routes, with their lane detail ("2 right lanes of 3 closed"). TomTom and 511NJ often describe
  the same crash; 511NJ is the official word, TomTom adds the delay estimate.
- **Slowdowns nearby**: jams and road works within a mile of the routes but not on them.
- Matching rules: an incident counts as "on" a route only if it runs along it in the same direction.
  The other carriageway shows as `opp. dir`; cross streets and planned closures off-route are dropped.
- For a future departure, TomTom uses its historic traffic model (typical traffic for that time), and
  only incidents whose window covers your departure are listed; scheduled ones say `starts h:mm`.
- The Open in Waze / Google Maps buttons at the top of the map's panel (and the links in the PDF)
  open the same trip on your phone for live navigation.

## Changing the addresses

Edit the four lines in `.env` (`HOSPITAL_LABEL`, `HOSPITAL_ADDRESS`, `HOME_LABEL`,
`HOME_ADDRESS`; the address is what TomTom geocodes, the label is what the reports show), and the
same names in the Streamlit app's Secrets. Delete `cache\geocode.json` if you change an address,
so it is looked up again. The first run prints the
address TomTom matched: check it.

## Trouble

- *No TomTom API key yet*: paste the key into `.env` (see above).
- *Missing HOSPITAL_ADDRESS / HOME_ADDRESS*: add the address lines to `.env` (see `.env.example`).
- *HTTP 403*: key wrong, not active yet, missing the Routing/Search/Traffic products, or the monthly
  free allowance is used up.
- *HTTP 429*: too many calls in a short burst; wait a minute.
- Run with `--json` and look at `cache\last_run.json` to see exactly what TomTom returned.
