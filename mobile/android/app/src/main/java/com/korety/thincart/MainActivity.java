package com.korety.thincart;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

/**
 * Capacitor host Activity. The customisations over the generated default are
 * registering the local plugins: AppUpdate, so the bundled launcher page can ask
 * what build it is and install the next one, and Calendar, so it can send the
 * phone's calendar to the server for travel detection. NOTE: `npx cap add
 * android` regenerates this file — keep the registerPlugin lines.
 */
public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(AppUpdatePlugin.class);
        registerPlugin(CalendarPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
