# ThinCart — Privacy Policy

_Last updated: **[YYYY-MM-DD]**_

This template is fill-in-the-blanks. Replace every **[bracketed placeholder]**,
publish it at a public URL, and paste that URL into the Google Play and Apple
App Store consoles. Have it reviewed by counsel before relying on it.

---

## 1. Who we are

ThinCart ("ThinCart", "we", "us") is a shared shopping-list app that learns
your household's repurchase cycles and suggests recipes and produce variety.
This policy explains what data we collect, why, and your choices.

- **Data controller:** **[Your name / legal entity]**
- **Contact email:** **[contact@example.com]**
- **Operating / hosting jurisdiction:** **[e.g. Japan / EU / United States]**

## 2. What we collect

We collect only what the app needs to work:

- **Account data** — your **email address** and a securely hashed password, used
  to sign you in and link you to your household.
- **Household & list data** — the shopping-list items you add, edit, complete, or
  remove; your household membership; and the **purchase history** used to learn
  repurchase cycles (e.g. how often you buy milk).
- **A display name** you optionally set for attribution within your household.
- **Technical data** — standard request metadata (IP address, timestamps) needed
  to operate and secure the service.

We do **not** collect location, contacts, advertising identifiers, or payment
information, and we do **not** use your data for advertising or cross-app
tracking.

## 3. Optional AI processing of item names

The recipe and produce-variety suggestions feature ("Ideas") sends **item names
from your list and recent purchases** to a large language model (LLM) to
generate recipe and diversity suggestions. This is used only to produce those
suggestions.

- LLM processing is performed by **[in-house / the model provider name, e.g.
  Anthropic or an OpenAI-compatible endpoint — state which]**.
- Only item names (and derived plant tokens) are sent — **not** your email or
  account identifiers.
- If you do not use the Ideas feature, no data is sent for LLM processing.

State your processor arrangement here: **[describe whether names leave your
infrastructure and to whom]**.

## 4. How we use your data

- Provide the core service: sync your shared list in real time and compute
  purchase-cycle reminders.
- Send **on-device reminders** ("You're probably due for milk"). These are
  scheduled locally on your device; we do **not** operate a push server.
- Maintain security, prevent abuse, and debug problems.

We rely on **[your legal basis — e.g. performance of a contract / your consent,
under GDPR/APPI as applicable]** to process this data.

## 5. Sharing

We do **not** sell your data. We share it only with:

- **Infrastructure providers** that host the service on our behalf (hosting,
  database), under confidentiality obligations — **[name provider(s)]**.
- The **LLM provider** described in §3, if applicable.
- Authorities where **required by law**.

Members of **your own household** can see the shared list and its item/purchase
data — that is the purpose of the app.

## 6. Retention

We keep your data while your account is active. When you delete your account
(see §7) we delete your personal data, subject to any retention required by law.
Backups are purged on a rolling **[e.g. 30-day]** cycle.

## 7. Your rights and account deletion

You can **delete your account and associated data from within the app** at any
time. This calls **`POST /api/account/delete`** and removes your account and
personal data from our systems.

Depending on your jurisdiction, you may also have rights to access, correct,
export, or restrict processing of your data, and to lodge a complaint with a
supervisory authority. To exercise these, contact **[contact@example.com]**.

## 8. Security

We protect your data in transit with TLS/HTTPS and store passwords using a
one-way hash. No system is perfectly secure, but we take reasonable measures to
protect your information.

## 9. Children

ThinCart is not directed to children under **[13 / 16, per your jurisdiction]**
and we do not knowingly collect their data.

## 10. Changes

We may update this policy; we will change the "Last updated" date above and,
for material changes, notify you in the app.

## 11. Contact

Questions or requests: **[contact@example.com]**
Operator: **[Your name / legal entity]**, **[operating jurisdiction]**.
