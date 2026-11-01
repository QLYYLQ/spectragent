package org.spectragent.device;

import android.net.LocalSocket;
import android.util.Log;
import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.function.Supplier;
import org.json.JSONObject;

/**
 * One host connection: the hello, then requests answered one at a time on this connection's
 * thread, in order. Answers and events leave through one queue that a writer thread drains in
 * order, so the thread that queues (the main thread, for events) never waits for the socket. An
 * event that would take the unsent events past {@link #MAX_UNSENT_EVENT_BYTES} is replaced by an
 * {@code overflow} frame, after which the connection closes: events are never dropped silently.
 */
final class Connection {
    private static final String TAG = "Spectragent";
    /** How long a new connection may take to send its hello. */
    private static final int HELLO_TIMEOUT_MS = 10_000;
    /**
     * The largest hello: anything on the device can connect, and a frame's arrays are allocated
     * from its declared length before the token is checked.
     */
    private static final int MAX_HELLO_BYTES = 64 * 1024;
    /** The unsent events a connection may hold: a host that stopped reading, if it is reached. */
    private static final long MAX_UNSENT_EVENT_BYTES = 32L * 1024 * 1024;
    /** How long the writer may take to send what is queued once the connection ends. */
    private static final long WRITER_FINISH_MS = 5_000;

    private static final class Outgoing {
        final byte[] frame;
        final boolean event;

        Outgoing(byte[] frame, boolean event) {
            this.frame = frame;
            this.event = event;
        }
    }

    private final LocalSocket socket;
    private final Handler handler;
    private final EventHub events;
    private final Supplier<String> token;
    private final JSONObject hello;

    private final ArrayDeque<Outgoing> queue = new ArrayDeque<>();
    private long unsentEventBytes;
    /** Nothing more is queued; the writer closes the stream once the queue is sent. */
    private boolean lastQueued;

    /** Events is null for the helper, which pushes none. */
    Connection(LocalSocket socket, Handler handler, EventHub events, Supplier<String> token, JSONObject hello) {
        this.socket = socket;
        this.handler = handler;
        this.events = events;
        this.token = token;
        this.hello = hello;
    }

    /** Serves the connection until the host closes it or breaks the protocol. */
    void serve() {
        Thread writer = new Thread(this::write, "spectragent-writer");
        writer.setDaemon(true);
        writer.start();
        try {
            DataInputStream in = new DataInputStream(new BufferedInputStream(socket.getInputStream()));
            if (greet(in)) {
                Frames.Frame frame;
                while ((frame = Frames.read(in)) != null) {
                    answer(frame.header);
                }
            }
        } catch (IOException ended) {
            Log.i(TAG, "a connection ended: " + ended.getMessage());
        } finally {
            if (events != null) {
                events.unsubscribe(this);
            }
            sendLast(null);
            try {
                writer.join(WRITER_FINISH_MS);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
            close();
        }
    }

    /** Closes the socket at once, which ends both threads. */
    void close() {
        try {
            socket.shutdownInput();
        } catch (IOException ignored) {
            // already shut
        }
        try {
            socket.close();
        } catch (IOException ignored) {
            // already closed
        }
    }

    void respond(long requestId, Reply reply) {
        send(Frames.response(requestId, reply));
    }

    synchronized void send(byte[] frame) {
        if (!lastQueued) {
            queue.add(new Outgoing(frame, false));
            notifyAll();
        }
    }

    synchronized void sendEvent(byte[] frame, long seq) {
        if (lastQueued) {
            return;
        }
        if (unsentEventBytes + frame.length > MAX_UNSENT_EVENT_BYTES) {
            Log.w(TAG, "a connection stopped reading; its events from " + seq + " on are not sent");
            queue.add(new Outgoing(Frames.encode(Json.obj("kind", "overflow", "next_seq", seq)), false));
            lastQueued = true;
            notifyAll();
            return;
        }
        unsentEventBytes += frame.length;
        queue.add(new Outgoing(frame, true));
        notifyAll();
    }

    /** Queues frame (if any) as the last one; the writer closes the stream after sending it. */
    synchronized void sendLast(byte[] frame) {
        if (frame != null && !lastQueued) {
            queue.add(new Outgoing(frame, false));
        }
        lastQueued = true;
        notifyAll();
    }

    private boolean greet(DataInputStream in) throws IOException {
        socket.setSoTimeout(HELLO_TIMEOUT_MS);
        Frames.Frame frame = Frames.read(in, MAX_HELLO_BYTES);
        socket.setSoTimeout(0);
        if (frame == null) {
            return false;
        }
        JSONObject header = frame.header;
        String reason = null;
        String message = null;
        String expected = token.get();
        if (!"hello".equals(header.optString("kind"))) {
            reason = "protocol";
            message = "the first frame must be the hello";
        } else if (header.optInt("protocol", -1) != BuildConfig.PROTOCOL_VERSION) {
            reason = "protocol";
            message = "this device speaks protocol " + BuildConfig.PROTOCOL_VERSION + " and the host speaks "
                    + header.opt("protocol") + ": install the APK built from the host's checkout";
        } else if (expected == null) {
            reason = "token";
            message = "no session token was set; the host sets one through the bootstrap provider first";
        } else if (!MessageDigest.isEqual(Frames.utf8(header.optString("token")), Frames.utf8(expected))) {
            reason = "token";
            message = "the token is not this session's";
        }
        if (reason != null) {
            sendLast(Frames.encode(Json.obj("kind", "refused", "reason", reason, "message", message)));
            return false;
        }
        send(Frames.encode(hello));
        return true;
    }

    private void answer(JSONObject header) throws IOException {
        long id = header.optLong("id", -1);
        String method = header.optString("method");
        if (!"request".equals(header.optString("kind")) || id < 0) {
            throw new IOException("a frame that is not a request: " + header);
        }
        JSONObject params = header.optJSONObject("params");
        if (params == null) {
            params = new JSONObject();
        }
        if ("subscribe".equals(method) && events != null) {
            events.subscribe(this, id);
            return;
        }
        try {
            respond(id, handler.handle(method, params));
        } catch (Failure failure) {
            send(Frames.error(id, failure.code, failure.getMessage()));
        } catch (RuntimeException bug) {
            Log.e(TAG, method + " failed", bug);
            send(Frames.error(id, "internal", bug.toString()));
        }
    }

    private void write() {
        try {
            OutputStream out = socket.getOutputStream();
            while (true) {
                Outgoing next;
                synchronized (this) {
                    while (queue.isEmpty() && !lastQueued) {
                        wait();
                    }
                    if (queue.isEmpty()) {
                        break;
                    }
                    next = queue.poll();
                    if (next.event) {
                        unsentEventBytes -= next.frame.length;
                    }
                }
                out.write(next.frame);
            }
            socket.shutdownOutput();
        } catch (IOException | InterruptedException ended) {
            Log.i(TAG, "a connection's writer ended: " + ended);
        }
    }
}
