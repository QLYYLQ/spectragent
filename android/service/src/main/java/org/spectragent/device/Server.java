package org.spectragent.device;

import android.net.LocalServerSocket;
import android.net.LocalSocket;
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

/** Listens on an abstract Unix socket and serves each host connection on a thread of its own. */
final class Server {
    private static final String TAG = "Spectragent";

    private final String name;
    private final Handler handler;
    private final EventHub events;
    private final Supplier<String> token;
    private final JSONObject hello;
    private final Set<Connection> connections = new HashSet<>();
    private LocalServerSocket listening;
    private volatile boolean stopped;

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
            Connection connection = new Connection(socket, handler, events, token, hello);
            synchronized (connections) {
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
}
