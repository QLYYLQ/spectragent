package org.spectragent.device;

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

    /** Numbers one event; encodes it only when a connection subscribed. */
    synchronized void publish(Encoder encoder) {
        long seq = nextSeq++;
        if (subscribers.isEmpty()) {
            return;
        }
        byte[] frame = encoder.encode(seq);
        for (Connection connection : subscribers) {
            connection.sendEvent(frame, seq);
        }
    }
}
