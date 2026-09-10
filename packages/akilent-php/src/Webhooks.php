<?php

declare(strict_types=1);

namespace Akilent;

final class Webhooks
{
    private const TOLERANCE_SECONDS = 300;

    /**
     * Verify an inbound `X-Akilent-Signature` header (`t=<unix>,v1=<hex>`)
     * over the exact raw request body.
     *
     * @throws SignatureVerificationError on any mismatch
     */
    public static function verify(
        string $payload,
        string $header,
        string $secret,
        int $toleranceSeconds = self::TOLERANCE_SECONDS
    ): bool {
        $parts = [];
        foreach (explode(',', $header) as $p) {
            $i = strpos($p, '=');
            if ($i !== false) {
                $parts[substr($p, 0, $i)] = substr($p, $i + 1);
            }
        }
        $timestamp = isset($parts['t']) ? (int) $parts['t'] : 0;
        $received = $parts['v1'] ?? '';
        if ($timestamp === 0 || $received === '') {
            throw new SignatureVerificationError('malformed signature header');
        }
        if (abs(time() - $timestamp) > $toleranceSeconds) {
            throw new SignatureVerificationError('timestamp outside tolerance window');
        }
        $expected = hash_hmac('sha256', $timestamp . '.' . $payload, $secret);
        if (!hash_equals($expected, $received)) {
            throw new SignatureVerificationError('signature mismatch');
        }
        return true;
    }
}
