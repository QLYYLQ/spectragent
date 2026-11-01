package org.spectragent.device;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import java.util.regex.Pattern;

/**
 * Bootstrap and health through {@code adb shell content call}, which needs no socket and answers
 * while the service's socket is down. Each call starts a shell process on the device, so nothing
 * large passes here. Only the shell and root users may call it.
 *
 * <ul>
 *   <li>{@code session}, arg the token: sets the session's token, which the service's hello then
 *       requires;
 *   <li>{@code health}: reports the build and the service's state.
 * </ul>
 *
 * Both answer a Bundle whose one key, {@code json}, holds the health report as JSON.
 */
public final class BootstrapProvider extends ContentProvider {
    private static final int ROOT_UID = 0;
    private static final int SHELL_UID = 2000;
    /** What survives a shell command line unquoted, and long enough to be a secret. */
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9_-]{16,}");

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        int caller = Binder.getCallingUid();
        if (caller != ROOT_UID && caller != SHELL_UID) {
            throw new SecurityException("only the shell and root users call this provider, not uid " + caller);
        }
        switch (method) {
            case "session":
                if (arg == null || !TOKEN.matcher(arg).matches()) {
                    throw new IllegalArgumentException("a session token is 16 or more of A-Z a-z 0-9 _ -");
                }
                Session.setToken(arg);
                break;
            case "health":
                break;
            default:
                throw new IllegalArgumentException("no method " + method + "; the methods are session and health");
        }
        Bundle result = new Bundle();
        result.putString("json", Session.health().toString());
        return result;
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
