package org.spectragent.device;

/** A request that failed: a code the host acts on, and a message for people. */
final class Failure extends Exception {
    final String code;

    Failure(String code, String message) {
        super(message);
        this.code = code;
    }
}
