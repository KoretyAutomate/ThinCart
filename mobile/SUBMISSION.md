# PlantCart — App Store submission runbook

This is the end-to-end runbook for shipping the PlantCart native wrapper to
**Google Play** and the **Apple App Store**. It assumes the Capacitor scaffold
in this `mobile/` directory. The app bundles the PWA (`app/`) and talks to the
**hosted** PlantCart API (not a local server) — see `api-base.js`.

---

## 0. Prerequisite code change (REQUIRED, not yet done)

The web layer currently hardcodes same-origin API/WS URLs
(`fetch('/api/...')`, `new WebSocket(...location.host...)`). A bundled native
app has no same-origin server, so **before any store build** `app/index.html`
must honor `window.PLANTCART_API_BASE`, falling back to same-origin for the web
build. The exact one-change approach (an `apiUrl()` / `wsUrl()` helper) is
documented in `mobile/api-base.js`. **This is a deliberate follow-up and is NOT
implemented yet.** Everything below assumes it has been wired and the backend
sends CORS for the app origin and accepts the JWT as an `Authorization: Bearer`
header (it already does).

Also required before submission:
- A **hosted privacy policy URL** (both stores require a public URL). Fill in
  and publish `PRIVACY.md`.
- App icons and store graphics (listed per store below).

---

## 1. Minimum-functionality / native-capability story (read first)

Apple **4.2** (and, less strictly, Play policy) rejects "just a website in a
wrapper." PlantCart's answer is the **on-device due-cycle local
notifications** feature (`mobile/local-notifications.md`): the app schedules
reminders like *"You're probably due for milk"* computed from `/api/cycles`,
entirely on-device via `@capacitor/local-notifications` — **no push server**.
Implement that feature before submitting; it is the concrete native capability
cited in the review notes.

### Social login caveat (design decision)
The MVP uses **email/password** auth only. **Do not** add Google or Apple
social sign-in casually: Apple guideline **4.8** makes **Sign in with Apple
mandatory** for any app that offers a third-party social login. Staying on
email/password for the MVP avoids that obligation entirely. Revisit only if/when
social login is genuinely needed, and budget the Sign in with Apple work then.

---

## 2. Google Play

### 2.1 Accounts & one-time setup (human)
- **Google Play Console** developer account — **$25 one-time** fee.
- Accept the Developer Distribution Agreement; set up the payments profile.

### 2.2 Signing keys (human — keep secret, never commit)
- Generate an **upload key** (keystore):
  ```bash
  keytool -genkey -v -keystore plantcart-upload.jks \
    -alias plantcart -keyalg RSA -keysize 2048 -validity 10000
  ```
- Enroll in **Play App Signing** (recommended): you upload with the *upload
  key*; Google holds the *app signing key*. Store the keystore + passwords in a
  password manager. Losing the upload key means resetting it via Google support.
- Wire the keystore into `android/app/build.gradle` `signingConfigs` (release),
  fed from CI secrets or a local `keystore.properties` — never checked in.

### 2.3 Build a release AAB (not APK)
Play requires an **Android App Bundle (.aab)** for production:
```bash
cd mobile
npm run sync                 # copy-web + cap sync android
cd android
./gradlew bundleRelease      # -> app/build/outputs/bundle/release/app-release.aab
```
- The CI workflow (`.github/workflows/build-android.yml`) builds a **debug
  APK** for smoke-testing only. The signed **release AAB** is produced with the
  release keystore (locally or in a secrets-fed CI job).
- Optional: inspect/generate a device APK set with **`bundletool`**
  (`bundletool build-apks --bundle=app-release.aab --output=out.apks`) to test
  the bundle before upload.

### 2.4 Store listing & policy forms (human)
- **Data safety form** — declare exactly what's collected and why. For
  PlantCart: **email** (account), **shopping-list / purchase data** (app
  function), whether data is encrypted in transit (yes), and that **account
  deletion** is available in-app (`POST /api/account/delete`). Note the optional
  LLM processing of item names by the recipe/diversity feature (`/api/ideas`).
- **Content rating** questionnaire (IARC) — PlantCart is a utility with no
  objectionable content; expect an "Everyone" rating.
- **Target audience & content**, ads declaration (no ads), news/COVID = no.
- **Store listing assets:**
  - App icon **512×512** PNG (32-bit, with alpha).
  - **Feature graphic 1024×500** PNG/JPG (required).
  - **Screenshots**: at least 2 phone screenshots (min 320px, 16:9 or 9:16);
    add 7-inch and 10-inch tablet screenshots if declaring tablet support.
  - Short description (≤80 chars) + full description.
- **Privacy policy URL** (from `PRIVACY.md`, hosted).

### 2.5 Release track order
1. **Internal testing** track first (up to 100 testers, near-instant) —
   validate signing, install, and the remote-API connection.
2. Then **Closed → Open testing** as needed.
3. **Production** after review. First-time apps also go through Google's
   identity/verification checks — allow lead time.

---

## 3. Apple App Store

> **The DGX cannot build iOS.** iOS archiving requires **Xcode on macOS**.
> You need a **Mac** (or a cloud-Mac service such as MacStadium / a GitHub
> macOS runner / Codemagic). Plan for this — it is the hard external dependency.

### 3.1 Accounts (human)
- **Apple Developer Program** membership — **$99/year**.
- Access to **App Store Connect** and the Developer portal.

### 3.2 Bundle ID & app record (human)
- Register the **bundle identifier `com.plantcart.app`** in the Developer portal
  (matches `appId` in `capacitor.config.json`).
- Create the app record in **App Store Connect** (name "PlantCart", primary
  language, bundle id).

### 3.3 Generate & open the iOS project (on the Mac)
```bash
cd mobile
npm ci
npm run sync            # copy-web + cap sync
npx cap add ios         # first time only, on macOS
npm run open:ios        # sync + cap open ios  -> opens Xcode
```
In Xcode:
- Set the **Signing team** (automatic signing with your Developer account).
- Confirm the bundle id is `com.plantcart.app`.
- **Product → Archive**, then **Distribute App → App Store Connect → Upload**.

### 3.4 Export compliance — Info.plist
Add to the iOS app's **Info.plist** so every submission skips the manual
encryption questionnaire (the app uses only standard HTTPS/TLS, which is exempt):
```xml
<key>ITSAppUsesNonExemptEncryption</key>
<false/>
```

### 3.5 App Privacy questionnaire (human, in App Store Connect)
- Declare data types: **email address** (linked to identity, for account),
  **user content** = shopping-list / purchase data (app functionality).
- State data is **not used for tracking**, encrypted in transit.
- Mention the optional **LLM processing of item names** (`/api/ideas`) if it
  leaves your infrastructure to a model provider — disclose the processor.
- **Account deletion** is available in-app (`POST /api/account/delete`) —
  required by App Store guideline 5.1.1(v) for apps with account creation.
- Provide the hosted **Privacy Policy URL**.

### 3.6 Demo reviewer account (REQUIRED — login-walled app)
PlantCart is **login-walled**: App Review cannot see anything without
credentials. Guideline **5.1.1(i)** requires you to provide a **working demo
account**. In App Store Connect → **App Review Information**:
- Supply a **demo email + password** for a pre-seeded household with sample
  list/cycle data (so the due-cycle notifications feature is demonstrable).
- Add reviewer **notes** explaining the local-notification feature and how to
  trigger it (this is the 4.2 minimum-functionality justification).
- Keep the demo account alive and its data non-empty for the review window.

### 3.7 Screenshots & metadata (human)
- Screenshots for required device sizes (6.7"/6.9" iPhone and, if supporting
  iPad, 12.9"/13" iPad) generated on the simulator or device.
- App description, keywords, support URL, marketing URL (optional), age rating
  questionnaire, category (Shopping or Productivity).

### 3.8 Submit
Attach the uploaded build, complete all metadata, and **Submit for Review**.

---

## 4. What only the human can do

These steps cannot be automated by this repo or the CI and are on the account
owner:

- **Create and pay for both developer accounts** — Google Play ($25 one-time),
  Apple Developer Program ($99/yr).
- **Provide a Mac (or cloud-Mac) with Xcode** for the iOS archive/upload — the
  DGX cannot build iOS at all.
- **Generate and safeguard signing material** — the Android upload keystore and
  Apple signing certificates/provisioning. Never commit these; store in a
  password manager / CI secrets.
- **Host the privacy policy** at a public URL (from `PRIVACY.md`) and paste that
  URL into both consoles.
- **Create and maintain the demo reviewer account** for Apple (login-walled).
- **Fill in the store forms** — Data Safety (Play), App Privacy (Apple),
  content/age ratings, listings, screenshots.
- **Press the final Submit** on each store and respond to any review feedback.

---

## 5. Quick checklist

- [ ] `index.html` honors `window.PLANTCART_API_BASE` (§0) — **follow-up**
- [ ] Local due-cycle notifications implemented (`local-notifications.md`)
- [ ] `PRIVACY.md` filled in and hosted; URL ready
- [ ] Backend CORS allows the app origin; JWT via `Authorization: Bearer`
- [ ] Android: upload keystore created; release AAB via `bundleRelease`
- [ ] Play: Data Safety, content rating, listing assets (512 icon, 1024×500
      feature graphic, screenshots), internal-testing track
- [ ] Apple: bundle id `com.plantcart.app` registered; `npx cap open ios` →
      archive → upload
- [ ] Apple: `ITSAppUsesNonExemptEncryption=false` in Info.plist
- [ ] Apple: App Privacy questionnaire + demo reviewer account (5.1.1(i))
- [ ] No social login (would trigger mandatory Sign in with Apple, 4.8)
