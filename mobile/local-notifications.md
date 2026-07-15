# Local due-cycle reminders (on-device, no push server)

**Status: scaffold-to-implement.** This is a design note plus a JS sketch. Nothing
here is wired into `app/index.html` yet. Implementing it is the concrete native
capability that carries the store submission (see below).

## Why this exists

Apple guideline **4.2 (Minimum Functionality)** rejects apps that are "just a
web page bundled in a wrapper." ThinCart's mitigation is a genuine native
feature that a website cannot provide: **local notifications** that remind the
household when an item is probably due to be repurchased — e.g. *"You're
probably due for milk."*

Crucially this uses **`@capacitor/local-notifications` only** — everything is
scheduled and fired **on-device**. There is **no push server, no FCM, no
APNs**. Nothing to host, no device tokens, no server-side scheduling. That
keeps the backend unchanged and sidesteps the whole push-notification review
surface (APNs entitlements, FCM keys) while still delivering a real native
capability.

## The data source

The backend already computes due-ness. `GET /api/cycles` returns:

```jsonc
{
  "cycles": [
    {
      "catalog_id": "milk",
      "name": "牛乳",         // display name (may be Japanese)
      "name_en": "milk",              // English alias for the notification text
      "label": "weekly",
      "median_days": 6.5,             // learned median repurchase interval
      "days_since": 5.9,              // days since last purchase
      "score": 0.91,                  // days_since / median_days
      "due": true,                    // server says this is in the due window now
      "on_list": false                // already on the shopping list?
    }
  ]
}
```

The native layer does **not** re-derive the statistics. It reads `median_days`
and `days_since`, projects the date each item crosses into the due window, and
schedules a local notification for that date. Items already `on_list` are
skipped (you don't nag about something already on the list).

## Permission + scheduling sketch

```js
import { LocalNotifications } from '@capacitor/local-notifications';

// The due window the server uses is score in [DUE_MIN, DUE_MAX] (server/cycles.py).
// Mirror the lower bound here so on-device projection matches server semantics.
const DUE_MIN = 0.8;

// 1) Ask for permission the first time (iOS requires an explicit grant).
async function ensureNotifPermission() {
  const perm = await LocalNotifications.checkPermissions();
  if (perm.display === 'granted') return true;
  const req = await LocalNotifications.requestPermissions();
  return req.display === 'granted';
}

// 2) Compute the local fire date for each cycle item and (re)schedule.
//    Call this after each successful /api/cycles fetch (e.g. on app resume).
async function scheduleDueReminders(cycles) {
  if (!(await ensureNotifPermission())) return;

  // Clear previously scheduled reminders so we don't stack duplicates.
  const pending = await LocalNotifications.getPending();
  if (pending.notifications.length) {
    await LocalNotifications.cancel({ notifications: pending.notifications });
  }

  const now = Date.now();
  const DAY = 86_400_000;
  const toSchedule = [];

  for (const c of cycles) {
    if (c.on_list) continue;                       // already handled
    // Days until the item reaches the due window's lower edge.
    const daysUntilDue = DUE_MIN * c.median_days - c.days_since;
    const fireAt = new Date(now + Math.max(0, daysUntilDue) * DAY);
    // If already due, fire shortly (a few minutes out), not in the past.
    if (daysUntilDue <= 0) fireAt.setTime(now + 5 * 60_000);

    const label = c.name_en || c.name;
    toSchedule.push({
      id: hashId(c.catalog_id),                    // stable small int per item
      title: 'ThinCart',
      body: `You're probably due for ${label}.`,
      schedule: { at: fireAt, allowWhileIdle: true },
      extra: { catalog_id: c.catalog_id },
    });
  }

  if (toSchedule.length) {
    await LocalNotifications.schedule({ notifications: toSchedule });
  }
}

// Capacitor notification ids must be 32-bit ints; hash the catalog string id.
function hashId(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % 2_147_483_647;
}

// 3) Tapping a reminder can deep-link into the list.
LocalNotifications.addListener('localNotificationActionPerformed', (ev) => {
  const cid = ev.notification.extra?.catalog_id;
  // e.g. focus/scroll the shopping list to this item, or add it.
});
```

## Wiring notes (for the implementer)

- **When to schedule.** Re-run `scheduleDueReminders()` on app launch and on
  resume (Capacitor `App` `resume` event), right after a fresh `/api/cycles`
  fetch. The list is small, so a full cancel-and-reschedule is simplest and
  keeps the schedule consistent with the latest server state.
- **Auth.** `/api/cycles` requires the `Authorization: Bearer <jwt>` header, so
  only schedule after login. On logout, cancel all pending notifications.
- **Only in the native build.** Guard calls with `Capacitor.isNativePlatform()`
  so the web PWA is unaffected (the plugin is a no-op / unavailable on web).
- **iOS icon/color** come from `capacitor.config.json` `LocalNotifications`
  plugin config (`ic_stat_thincart`, `#2f6f4f`). Provide the small-icon asset
  in the Android project when generating it.
- **Quiet hours (optional polish).** Clamp `fireAt` to daytime so reminders
  don't fire at 3am.
```
