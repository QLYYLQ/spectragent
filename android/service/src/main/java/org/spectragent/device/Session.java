package org.spectragent.device;

import android.os.Build;
import org.json.JSONObject;

/**
 * What the bootstrap provider and the service share in the app's process: the session's token
 * and the service's state. Android components of one process share state this way; the token
 * lives only in memory, so a restarted process needs a new one from the host.
 */
final class Session {
    private static String token;
    private static boolean serviceConnected;
    private static boolean listening;

    private Session() {}

    static synchronized void setToken(String value) {
        token = value;
    }

    static synchronized String token() {
        return token;
    }

    static synchronized void service(boolean connected, boolean isListening) {
        serviceConnected = connected;
        listening = isListening;
    }

    /** The provider's health report; binding itself is confirmed by a ping on the socket. */
    static synchronized JSONObject health() {
        return Json.obj(
                "protocol", BuildConfig.PROTOCOL_VERSION,
                "package", BuildConfig.APPLICATION_ID,
                "version_code", BuildConfig.VERSION_CODE,
                "version_name", BuildConfig.VERSION_NAME,
                "accessibility_tool", BuildConfig.ACCESSIBILITY_TOOL,
                "sdk_int", Build.VERSION.SDK_INT,
                "socket", DeviceService.SOCKET,
                "service_connected", serviceConnected,
                "listening", listening,
                "token_set", token != null);
    }
}
