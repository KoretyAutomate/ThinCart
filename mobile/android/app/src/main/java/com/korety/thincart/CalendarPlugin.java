package com.korety.thincart;

import android.Manifest;
import android.content.ContentUris;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;
import android.util.Log;

import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Travel detection's calendar source: read the calendar Android already syncs
 * and send it to the ThinCart server (PLAN.md §2026-09-26).
 *
 *   Plugins.Calendar.checkPermissions()                 -> {calendar: "granted" | "prompt" | ...}
 *   Plugins.Calendar.requestPermissions()               -> same shape
 *   Plugins.Calendar.push({url})                        -> {queued: true}
 *
 * Why not @ebarooni/capacitor-calendar, which OutfitAdvisor uses: it queries
 * Events with DTSTART >= from AND DTEND <= to, so a recurring event arrives
 * once or not at all (its DTEND is null) and anything already under way is
 * dropped. Instances expands recurrences and returns whatever OVERLAPS the
 * window.
 *
 * What leaves the phone: ALL-DAY events only, from calendars the user owns.
 * A timed event is never travel to the server's detector, so sending one would
 * be private data for nothing.
 *
 * push() only posts to a host the app is already allowed to navigate to, and
 * only to /api/calendar/events. The Capacitor bridge may be reachable from the
 * tailnet page as well as the launcher, so this must not be a "send my
 * calendar anywhere" primitive.
 */
@CapacitorPlugin(
    name = "Calendar",
    permissions = { @Permission(alias = "calendar", strings = { Manifest.permission.READ_CALENDAR }) }
)
public class CalendarPlugin extends Plugin {

    private static final String TAG = "ThinCartCalendar";
    private static final String PATH = "/api/calendar/events";

    // Must match away.WINDOW_BACK_DAYS / WINDOW_AHEAD_DAYS on the server, which
    // clamps to them anyway — a wider read would only be wasted.
    static final long BACK_DAYS = 180;
    static final long AHEAD_DAYS = 30;
    private static final long DAY_MS = 24L * 60 * 60 * 1000;

    private static final int MAX_EVENTS = 5000;
    private static final int TIMEOUT_MS = 15000;

    // One background thread: a push survives the launcher navigating away,
    // because this plugin instance lives as long as the Activity, not the page.
    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    @PluginMethod
    public void push(PluginCall call) {
        final String base = call.getString("url");
        final URL target;
        try {
            target = target(base);
        } catch (Exception e) {
            call.reject(e.getMessage());
            return;
        }
        if (ContextCompat.checkSelfPermission(getContext(), Manifest.permission.READ_CALENDAR)
                != PackageManager.PERMISSION_GRANTED) {
            call.reject("calendar permission not granted");
            return;
        }
        // Resolve now: the launcher is about to hand over to the server, and
        // nothing on the page needs the result — the server records it.
        call.resolve(new JSObject().put("queued", true));
        executor.execute(() -> {
            try {
                long now = System.currentTimeMillis();
                long from = now - BACK_DAYS * DAY_MS;
                long to = now + AHEAD_DAYS * DAY_MS;
                List<Long> calendars = ownedCalendars();
                JSONArray events = calendars.isEmpty() ? new JSONArray() : allDayEvents(calendars, from, to);
                JSONObject body = new JSONObject();
                body.put("window", new JSONObject().put("start", isoUtc(from)).put("end", isoUtc(to)));
                body.put("calendars", calendars.size());
                body.put("events", events);
                int status = post(target, body.toString());
                Log.i(TAG, "pushed " + events.length() + " events from " + calendars.size()
                        + " calendars -> HTTP " + status);
            } catch (Exception e) {
                Log.w(TAG, "calendar push failed", e);
            }
        });
    }

    /** The server's endpoint, if the given base is a host this app may visit. */
    private URL target(String base) throws Exception {
        if (base == null || base.trim().isEmpty()) throw new IllegalArgumentException("url required");
        URL u = new URL(base.trim());
        String scheme = u.getProtocol();
        if (!"https".equals(scheme) && !"http".equals(scheme)) throw new IllegalArgumentException("bad scheme");
        if (!getBridge().getAppAllowNavigationMask().matches(u.getHost())) {
            throw new IllegalArgumentException("host not allowed: " + u.getHost());
        }
        return new URL(scheme, u.getHost(), u.getPort(), PATH);
    }

    /** Calendars the user owns: their primary, and calendars they created. */
    private List<Long> ownedCalendars() {
        List<Long> ids = new ArrayList<>();
        String[] cols = {
            CalendarContract.Calendars._ID,
            CalendarContract.Calendars.ACCOUNT_NAME,
            CalendarContract.Calendars.OWNER_ACCOUNT,
            CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL,
        };
        try (Cursor c = getContext().getContentResolver().query(
                CalendarContract.Calendars.CONTENT_URI, cols, null, null, null)) {
            if (c == null) return ids;
            while (c.moveToNext()) {
                if (isOwned(c.getInt(3), c.getString(2), c.getString(1))) ids.add(c.getLong(0));
            }
        }
        return ids;
    }

    /**
     * Owner access, AND either the account's own calendar (owner == account) or
     * one the user created (Google gives those a group-calendar id as owner).
     * A colleague's calendar shared with "make changes and manage sharing" also
     * reaches owner access, but its owner is the colleague's address — out.
     * Holidays and contacts' birthdays are read-only — out.
     */
    static boolean isOwned(int access, String owner, String account) {
        if (access < CalendarContract.Calendars.CAL_ACCESS_OWNER || owner == null) return false;
        return owner.equalsIgnoreCase(account == null ? "" : account)
                || owner.toLowerCase(Locale.ROOT).endsWith("@group.calendar.google.com");
    }

    private JSONArray allDayEvents(List<Long> calendars, long from, long to) throws Exception {
        Uri.Builder b = CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(b, from);
        ContentUris.appendId(b, to);
        String[] cols = {
            CalendarContract.Instances.EVENT_ID,
            CalendarContract.Instances.BEGIN,
            CalendarContract.Instances.END,
            CalendarContract.Instances.TITLE,
            CalendarContract.Instances.EVENT_LOCATION,
            CalendarContract.Instances.STATUS,
            CalendarContract.Instances.SELF_ATTENDEE_STATUS,
        };
        StringBuilder in = new StringBuilder();
        for (Long id : calendars) in.append(in.length() == 0 ? "" : ",").append(id);
        String where = CalendarContract.Instances.ALL_DAY + "=1 AND "
                + CalendarContract.Instances.CALENDAR_ID + " IN (" + in + ")";
        JSONArray out = new JSONArray();
        try (Cursor c = getContext().getContentResolver().query(b.build(), cols, where, null,
                CalendarContract.Instances.BEGIN)) {
            if (c == null) return out;
            while (c.moveToNext() && out.length() < MAX_EVENTS) {
                long begin = c.getLong(1);
                JSONObject ev = new JSONObject();
                // one id per occurrence: a weekly all-day event is many trips, not one
                ev.put("id", c.getLong(0) + ":" + begin);
                ev.put("summary", nz(c.getString(3)));
                ev.put("location", nz(c.getString(4)));
                ev.put("start", new JSONObject().put("date", allDayDate(begin)));
                ev.put("end", new JSONObject().put("date", allDayDate(c.getLong(2))));
                if (c.getInt(5) == CalendarContract.Events.STATUS_CANCELED) ev.put("status", "cancelled");
                if (c.getInt(6) == CalendarContract.Attendees.ATTENDEE_STATUS_DECLINED) {
                    ev.put("attendees", new JSONArray().put(
                        new JSONObject().put("self", true).put("responseStatus", "declined")));
                }
                out.put(ev);
            }
        }
        return out;
    }

    /**
     * Android stores all-day bounds at UTC midnight, so the date is read in UTC.
     * Reading it in local time — as OutfitAdvisor does — puts every all-day
     * event a day early anywhere west of Greenwich. END stays exclusive, which
     * is exactly Google's all-day convention the server already handles.
     */
    static String allDayDate(long epochMs) {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(epochMs);
    }

    static String isoUtc(long epochMs) {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.ROOT);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(epochMs);
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }

    private static int post(URL target, String json) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) target.openConnection();
        try {
            conn.setConnectTimeout(TIMEOUT_MS);
            conn.setReadTimeout(TIMEOUT_MS);
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            try (OutputStream os = conn.getOutputStream()) {
                os.write(json.getBytes(StandardCharsets.UTF_8));
            }
            return conn.getResponseCode();
        } finally {
            conn.disconnect();
        }
    }
}
