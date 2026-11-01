package org.spectragent.device;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.util.Log;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.regex.Pattern;

/**
 * Bootstrap and health through {@code adb shell content}, which needs no socket and answers while
 * the service's socket is down. Each use starts a shell process on the device, so nothing large
 * passes here. Only the shell and root users may use it.
 *
 * <ul>
 *   <li>{@code content write --uri content://org.spectragent.device.bootstrap/session}, the token
 *       on its stdin: sets the session's token, which the service's hello then requires. The token
 *       never appears on a command line, where {@code ps} would show it;
 *   <li>{@code content call --method health}: a Bundle whose one key, {@code json}, holds the build
 *       and the service's state as JSON.
 * </ul>
 */
public final class BootstrapProvider extends ContentProvider {
    private static final String TAG = "Spectragent";
    private static final int ROOT_UID = 0;
    private static final int SHELL_UID = 2000;
    private static final String SESSION = "/session";
    /** What a session token is: long enough to be a secret. */
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9_-]{16,128}");

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        checkCaller();
        if (!"health".equals(method)) {
            throw new IllegalArgumentException("no method " + method + "; the method is health, and the token is written to "
                    + SESSION);
        }
        Bundle result = new Bundle();
        result.putString("json", Session.health().toString());
        return result;
    }

    /** The session's token, written to {@code /session}: read from a pipe until the writer closes it. */
    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        checkCaller();
        if (!SESSION.equals(uri.getPath()) || !mode.contains("w")) {
            throw new FileNotFoundException("only " + SESSION + " opens, for writing the session's token");
        }
        ParcelFileDescriptor[] pipe;
        try {
            pipe = ParcelFileDescriptor.createPipe();
        } catch (IOException failed) {
            throw new FileNotFoundException("no pipe for the token: " + failed);
        }
        ParcelFileDescriptor reading = pipe[0];
        Thread reader = new Thread(() -> readToken(reading), "spectragent-token");
        reader.setDaemon(true);
        reader.start();
        return pipe[1];
    }

    private static void readToken(ParcelFileDescriptor reading) {
        try (InputStream in = new ParcelFileDescriptor.AutoCloseInputStream(reading)) {
            byte[] buffer = new byte[256];
            int total = 0;
            int read;
            while (total < buffer.length && (read = in.read(buffer, total, buffer.length - total)) != -1) {
                total += read;
            }
            String token = new String(buffer, 0, total, StandardCharsets.UTF_8).trim();
            if (TOKEN.matcher(token).matches()) {
                Session.setToken(token);
            } else {
                Log.w(TAG, "refused a session token: it is 16 to 128 of A-Z a-z 0-9 _ -");
            }
        } catch (IOException failed) {
            Log.w(TAG, "could not read the session token: " + failed);
        }
    }

    private static void checkCaller() {
        int caller = Binder.getCallingUid();
        if (caller != ROOT_UID && caller != SHELL_UID) {
            throw new SecurityException("only the shell and root users use this provider, not uid " + caller);
        }
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
