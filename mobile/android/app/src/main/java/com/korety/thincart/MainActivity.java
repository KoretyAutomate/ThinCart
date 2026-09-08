package com.korety.thincart;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

/**
 * Capacitor host Activity. The one customisation over the generated default is
 * registering the local AppUpdate plugin, so the bundled launcher page can ask
 * what build it is and install the next one. NOTE: `npx cap add android`
 * regenerates this file — keep the registerPlugin line.
 */
public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(AppUpdatePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
