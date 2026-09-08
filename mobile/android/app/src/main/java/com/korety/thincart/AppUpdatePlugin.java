package com.korety.thincart;

import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

/**
 * In-app updates — "there's a new build, tap to install" — without Play.
 *
 * A Java port of OutfitAdvisor's AppUpdatePlugin (this shell is Java-only and
 * adding Kotlin to the build for one class is not worth it). Same contract:
 *
 *   Plugins.AppUpdate.current()                  -> {versionCode, versionName, canInstall}
 *   Plugins.AppUpdate.install({url, sha256, size}) -> {status: "installer-opened" | "needs-permission"}
 *
 * What makes it safe: Android will only let a package REPLACE this app if it
 * carries the same signing key — the CI cert-drift gate keeps every build on
 * the persistent key — so a hostile server address could at worst offer a
 * separate install with a visible prompt, never a silent takeover. The sha256
 * from /version is checked before the file reaches the installer, so a
 * truncated download is refused rather than handed over.
 *
 * Only the bundled launcher page can reach this: Capacitor injects the native
 * bridge for the app's own origin, not for the tailnet page it hands over to.
 * That is why the update UI lives in the launcher and not in the list.
 */
@CapacitorPlugin(name = "AppUpdate")
public class AppUpdatePlugin extends Plugin {

    // The real APK is ~3.5 MB; a generous ceiling that still bounds a runaway response.
    private static final long MAX_APK_BYTES = 100L * 1024 * 1024;

    /** What this build is, so the launcher can compare against GET /version. */
    @PluginMethod
    public void current(PluginCall call) {
        try {
            android.content.pm.PackageInfo info =
                getContext().getPackageManager().getPackageInfo(getContext().getPackageName(), 0);
            long code = Build.VERSION.SDK_INT >= 28 ? info.getLongVersionCode() : info.versionCode;
            JSObject out = new JSObject();
            out.put("versionCode", (int) code);
            out.put("versionName", info.versionName == null ? "" : info.versionName);
            out.put("canInstall", canInstall());
            call.resolve(out);
        } catch (Exception e) {
            call.reject("could not read the package: " + e.getMessage());
        }
    }

    /** Download, verify, and hand to the system installer — off the main thread. */
    @PluginMethod
    public void install(PluginCall call) {
        final String url = call.getString("url");
        if (url == null || url.trim().isEmpty()) { call.reject("url required"); return; }
        final String expectedSha = call.getString("sha256") == null ? null : call.getString("sha256").toLowerCase();
        final Long sizeArg = call.getLong("size");
        final long expectedSize = sizeArg == null ? 0L : sizeArg;

        if (!canInstall()) {
            // Android 8+: installing from a non-store source is a per-app grant made
            // in Settings. There is no in-app dialog for it by OS design, so route
            // there and let the launcher re-check on resume.
            openInstallPermissionSettings();
            call.resolve(new JSObject().put("status", "needs-permission"));
            return;
        }

        new Thread(() -> {
            HttpURLConnection conn = null;
            File out = new File(getContext().getCacheDir(), "update.apk");
            try {
                out.delete();
                conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setConnectTimeout(10_000);
                conn.setReadTimeout(60_000);
                if (conn.getResponseCode() != 200) throw new IllegalStateException("server " + conn.getResponseCode());

                MessageDigest digest = MessageDigest.getInstance("SHA-256");
                long total = 0L;
                try (InputStream in = conn.getInputStream(); FileOutputStream sink = new FileOutputStream(out)) {
                    byte[] buf = new byte[64 * 1024];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        digest.update(buf, 0, n);
                        sink.write(buf, 0, n);
                        total += n;
                        if (total > MAX_APK_BYTES) throw new IllegalStateException("apk too large");
                    }
                }
                if (expectedSize > 0 && total != expectedSize)
                    throw new IllegalStateException("size mismatch: got " + total + ", expected " + expectedSize);
                StringBuilder sb = new StringBuilder();
                for (byte b : digest.digest()) sb.append(String.format("%02x", b));
                if (expectedSha != null && !expectedSha.isEmpty() && !sb.toString().equals(expectedSha))
                    throw new IllegalStateException("checksum mismatch — download corrupted");

                Uri uri = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", out);
                Intent intent = new Intent(Intent.ACTION_VIEW);
                intent.setDataAndType(uri, "application/vnd.android.package-archive");
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(intent);
                call.resolve(new JSObject().put("status", "installer-opened").put("bytes", total));
            } catch (Exception e) {
                out.delete();
                call.reject(e.getMessage() == null ? "download failed" : e.getMessage());
            } finally {
                if (conn != null) conn.disconnect();
            }
        }).start();
    }

    /** Send the user to the "Install unknown apps" screen for THIS app. */
    @PluginMethod
    public void requestInstallPermission(PluginCall call) {
        if (canInstall()) { call.resolve(new JSObject().put("granted", true)); return; }
        openInstallPermissionSettings();
        call.resolve(new JSObject().put("granted", false));
    }

    private boolean canInstall() {
        return Build.VERSION.SDK_INT < 26 || getContext().getPackageManager().canRequestPackageInstalls();
    }

    private void openInstallPermissionSettings() {
        if (Build.VERSION.SDK_INT < 26) return;
        try {
            getContext().startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:" + getContext().getPackageName())).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (Exception e) {
            // Some OEM builds lack the per-app screen; the global list is the fallback.
            try {
                getContext().startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Exception ignored) { }
        }
    }
}
