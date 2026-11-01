package org.spectragent.device;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Process;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.util.Log;
import java.io.IOException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Supplier;
import org.json.JSONObject;

/**
 * Listens on an abstract Unix socket and serves each host connection on a thread of its own.
 *
 * <p>Any app on the device can connect to the name, but the host arrives only through adbd, so a
 * connection whose peer is not the shell or root user ({@link #isHostUid}) is closed as soon as it
 * is accepted. Behind that, each connection holds two threads until its hello times out, so at
 * most {@link #MAX_CONNECTIONS} are open at once, and one more is closed as soon as it is accepted,
 * before it is read from.
 */
final class Server {
    private static final String TAG = "Spectragent";
    /** The host needs one or two; the rest of the room is for the moment a closed one lingers. */
    static final int MAX_CONNECTIONS = 8;

    private final String name;
    private final Handler handler;
    private final EventHub events;
    private final Supplier<String> token;
    private final JSONObject hello;
    private final Set<Connection> connections = new HashSet<>();
    private LocalServerSocket listening;
    private volatile boolean stopped;

    /**
     * Whether a uid may use the device side: the shell user, as which adbd runs, or root, after
     * {@code adb root}. The host arrives only through adbd; the bootstrap provider applies the same
     * rule to its callers.
     */
    static boolean isHostUid(int uid) {
        return uid == Process.SHELL_UID || uid == Process.ROOT_UID;
    }

    /** Events is null for the helper, which pushes none. */
    Server(String name, Handler handler, EventHub events, Supplier<String> token, JSONObject hello) {
        this.name = name;
        this.handler = handler;
        this.events = events;
        this.token = token;
        this.hello = hello;
    }

    /** Binds the socket; throws when the name is taken. */
    void start() throws IOException {
        listening = new LocalServerSocket(name);
        Thread accepting = new Thread(this::accept, "spectragent-accept");
        accepting.setDaemon(true);
        accepting.start();
    }

    /** Stops listening and closes every connection. Safe to repeat. */
    void stop() {
        stopped = true;
        LocalServerSocket socket = listening;
        if (socket != null) {
            try {
                // close() alone does not wake a thread blocked in accept().
                Os.shutdown(socket.getFileDescriptor(), OsConstants.SHUT_RDWR);
            } catch (ErrnoException ignored) {
                // not listening any more
            }
            try {
                socket.close();
            } catch (IOException ignored) {
                // already closed
            }
        }
        List<Connection> open;
        synchronized (connections) {
            open = new ArrayList<>(connections);
        }
        for (Connection connection : open) {
            connection.close();
        }
    }

    private void accept() {
        while (!stopped) {
            LocalSocket socket;
            try {
                socket = listening.accept();
            } catch (IOException failed) {
                if (!stopped) {
                    Log.e(TAG, "stopped accepting on " + name, failed);
                }
                return;
            }
            int uid = peerUid(socket);
            if (!isHostUid(uid)) {
                Log.w(TAG, name + " closed a connection from uid " + uid + ": only the shell and root users connect");
                closeQuietly(socket);
                continue;
            }
            Connection connection = new Connection(socket, handler, events, token, hello);
            synchronized (connections) {
                if (connections.size() >= MAX_CONNECTIONS) {
                    Log.w(TAG, name + " has " + MAX_CONNECTIONS + " connections open; closed another");
                    connection.close();
                    continue;
                }
                connections.add(connection);
            }
            Thread serving = new Thread(() -> {
                try {
                    connection.serve();
                } finally {
                    synchronized (connections) {
                        connections.remove(connection);
                    }
                }
            }, "spectragent-connection");
            serving.setDaemon(true);
            serving.start();
        }
    }

    /** The uid of the process at the other end, or -1 when the system does not say. */
    private static int peerUid(LocalSocket socket) {
        try {
            return socket.getPeerCredentials().getUid();
        } catch (IOException unknown) {
            return -1;
        }
    }

    private static void closeQuietly(LocalSocket socket) {
        try {
            socket.close();
        } catch (IOException ignored) {
            // closed already
        }
    }
}
