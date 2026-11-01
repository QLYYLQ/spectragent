package org.spectragent.device;

import android.graphics.Bitmap;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.zip.CRC32;
import java.util.zip.Deflater;
import java.util.zip.DeflaterOutputStream;

/**
 * Screenshots as PNG bytes, written here rather than by Bitmap.compress: on the emulator the
 * capture took about 16 ms and Bitmap.compress about 560-1000 ms (2026-11-01), and it has no
 * compression setting. This writes the pixels losslessly as 8-bit RGBA rows without a filter,
 * deflated at zlib's fastest level, which trades a larger file for a much faster encoding.
 */
final class Images {
    private static final byte[] SIGNATURE = {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};

    private Images() {}

    /** The bitmap as a PNG; a hardware bitmap (a screenshot's) is copied to memory first. */
    static byte[] png(Bitmap bitmap) {
        Bitmap readable = bitmap.getConfig() == Bitmap.Config.ARGB_8888
                ? bitmap
                : bitmap.copy(Bitmap.Config.ARGB_8888, false);
        if (readable == null) {
            throw new IllegalStateException("the screenshot could not be copied out of graphics memory");
        }
        try {
            int width = readable.getWidth();
            int height = readable.getHeight();
            int row = width * 4;
            // ARGB_8888 is laid out in memory as R, G, B, A bytes.
            ByteBuffer pixels = ByteBuffer.allocate(row * height);
            readable.copyPixelsToBuffer(pixels);
            byte[] rgba = pixels.array();
            byte[] scanlines = new byte[(row + 1) * height]; // each row: filter 0 (none), its pixels
            for (int y = 0; y < height; y++) {
                System.arraycopy(rgba, y * row, scanlines, y * (row + 1) + 1, row);
            }
            ByteArrayOutputStream png = new ByteArrayOutputStream(scanlines.length / 4);
            png.write(SIGNATURE, 0, SIGNATURE.length);
            chunk(png, "IHDR", ByteBuffer.allocate(13).putInt(width).putInt(height)
                    .put((byte) 8).put((byte) 6).put((byte) 0).put((byte) 0).put((byte) 0).array());
            chunk(png, "IDAT", deflate(scanlines));
            chunk(png, "IEND", new byte[0]);
            return png.toByteArray();
        } finally {
            if (readable != bitmap) {
                readable.recycle();
            }
        }
    }

    private static byte[] deflate(byte[] data) {
        Deflater deflater = new Deflater(Deflater.BEST_SPEED);
        ByteArrayOutputStream out = new ByteArrayOutputStream(data.length / 4);
        try (DeflaterOutputStream zlib = new DeflaterOutputStream(out, deflater, 64 * 1024)) {
            zlib.write(data);
        } catch (IOException impossible) { // written to memory
            throw new IllegalStateException(impossible);
        } finally {
            deflater.end();
        }
        return out.toByteArray();
    }

    private static void chunk(ByteArrayOutputStream png, String type, byte[] data) {
        byte[] name = Frames.utf8(type);
        CRC32 crc = new CRC32();
        crc.update(name);
        crc.update(data);
        png.write(ByteBuffer.allocate(4).putInt(data.length).array(), 0, 4);
        png.write(name, 0, 4);
        png.write(data, 0, data.length);
        png.write(ByteBuffer.allocate(4).putInt((int) crc.getValue()).array(), 0, 4);
    }
}
