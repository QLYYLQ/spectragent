package org.spectragent.device;

import android.os.Build;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import org.json.JSONObject;

/** How a program describes itself in its hello: what was built, and what it runs from. */
final class Identity {
    private Identity() {}

    /**
     * The hello's fields. The versions are the ones compiled into the running code, so they are
     * the installed APK's; apkPath is the APK the code was loaded from, hashed on the device.
     * accessibilityTool is the system's reading of the service (null for the helper, and before
     * API 31, where the attribute does not exist).
     */
    static JSONObject hello(String component, String apkPath, Boolean accessibilityTool) {
        return Json.obj(
                "kind", "hello",
                "protocol", BuildConfig.PROTOCOL_VERSION,
                "component", component,
                "package", BuildConfig.APPLICATION_ID,
                "version_code", BuildConfig.VERSION_CODE,
                "version_name", BuildConfig.VERSION_NAME,
                "sdk_int", Build.VERSION.SDK_INT,
                "apk_sha256", sha256(apkPath),
                "accessibility_tool", accessibilityTool);
    }

    static String sha256(String path) {
        try (InputStream in = new FileInputStream(path)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) != -1) {
                digest.update(buffer, 0, read);
            }
            StringBuilder hex = new StringBuilder(64);
            for (byte b : digest.digest()) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (IOException | NoSuchAlgorithmException unreadable) {
            throw new IllegalStateException("cannot hash " + path, unreadable);
        }
    }
}
