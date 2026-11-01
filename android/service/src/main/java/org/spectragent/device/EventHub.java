package org.spectragent.device;

import android.util.Log;
import java.util.ArrayList;
import java.util.List;

/**
 * Numbers the service's events, from 0 for the service's life, and queues each on every
 * subscribed connection, in order. A subscription is answered under the same lock, so its
 * answer is queued before any event it covers.
 */
final class EventHub {
    interface Encoder {
        byte[] encode(long seq);
    }

    private final List<Connection> subscribers = new ArrayList<>();
    private long nextSeq;

    synchronized void subscribe(Connection connection, long requestId) {
        if (!subscribers.contains(connection)) {
            subscribers.add(connection);
        }
        connection.respond(requestId, Reply.of(Json.obj("next_seq", nextSeq)));
    }

    synchronized void unsubscribe(Connection connection) {
        subscribers.remove(connection);
    }

    synchronized long nextSeq() {
        return nextSeq;
    }

    /**
     * Numbers one event; encodes it only when a connection subscribed. An event that cannot be
     * encoded is logged and not sent: its subscribers see the gap in the numbering, and their
     * streams end there, explicitly. Nothing is thrown on the thread delivering events.
     */
    synchronized void publish(Encoder encoder) {
        long seq = nextSeq++;
        if (subscribers.isEmpty()) {
            return;
        }
        byte[] frame;
        try {
            frame = encoder.encode(seq);
        } catch (RuntimeException unencodable) {
            Log.e("Spectragent", "event " + seq + " could not be encoded; its subscribers see a gap", unencodable);
            return;
        }
        for (Connection connection : subscribers) {
            connection.sendEvent(frame, seq);
        }
    }
}
