<?php

declare(strict_types=1);

namespace Akilent;

/**
 * curl-based HTTP transport: auth, retry/backoff on 429/5xx, auto Idempotency-Key
 * on POST. Override {@see Transport::$sender} in tests to avoid real network I/O.
 */
class Transport
{
    public const DEFAULT_BASE_URL = 'https://akilent.com';
    private const USER_AGENT = 'akilent-php/0.1.0';
    private const RETRY_STATUSES = [429, 500, 502, 503, 504];

    private string $apiKey;
    private string $baseUrl;
    private int $timeout;
    private int $maxRetries;

    /** @var null|callable(string,string,array<string,string>,?string):array{0:int,1:string,2:array<string,string>} */
    public $sender = null;

    public function __construct(
        string $apiKey,
        string $baseUrl = self::DEFAULT_BASE_URL,
        int $timeout = 30,
        int $maxRetries = 2
    ) {
        if ($apiKey === '') {
            throw new \InvalidArgumentException('api_key is required');
        }
        $this->apiKey = $apiKey;
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->timeout = $timeout;
        $this->maxRetries = $maxRetries;
    }

    /**
     * @param array<string,mixed>|null $query
     * @param array<string,mixed>|null $json
     * @return mixed decoded JSON body
     */
    public function request(string $method, string $path, ?array $query = null, ?array $json = null, ?string $idempotencyKey = null)
    {
        $method = strtoupper($method);
        $url = $this->baseUrl . $path;
        if ($query) {
            $url .= '?' . http_build_query(array_filter($query, static fn ($v) => $v !== null));
        }

        $headers = [
            'Authorization' => 'Bearer ' . $this->apiKey,
            'User-Agent' => self::USER_AGENT,
            'Accept' => 'application/json',
        ];
        if ($method === 'POST') {
            $headers['Idempotency-Key'] = $idempotencyKey ?? self::uuid4();
        }
        $bodyStr = null;
        if ($json !== null) {
            $headers['Content-Type'] = 'application/json';
            $bodyStr = json_encode($json, JSON_UNESCAPED_SLASHES);
        }

        $attempt = 0;
        while (true) {
            try {
                [$status, $rawBody, $respHeaders] = $this->send($method, $url, $headers, $bodyStr);
            } catch (ApiConnectionError $e) {
                if ($attempt < $this->maxRetries) {
                    $attempt++;
                    self::sleep(self::backoff($attempt));
                    continue;
                }
                throw $e;
            }

            if (in_array($status, self::RETRY_STATUSES, true) && $attempt < $this->maxRetries) {
                $attempt++;
                self::sleep(self::retryDelay($respHeaders, $attempt));
                continue;
            }

            $decoded = $rawBody === '' ? null : json_decode($rawBody, true);
            if ($status >= 400) {
                throw ErrorFactory::fromResponse($status, $decoded, $respHeaders);
            }
            return $decoded;
        }
    }

    /**
     * @param array<string,string> $headers
     * @return array{0:int,1:string,2:array<string,string>}
     */
    private function send(string $method, string $url, array $headers, ?string $body): array
    {
        if ($this->sender !== null) {
            return ($this->sender)($method, $url, $headers, $body);
        }

        $ch = curl_init($url);
        $flat = [];
        foreach ($headers as $k => $v) {
            $flat[] = "$k: $v";
        }
        curl_setopt_array($ch, [
            CURLOPT_CUSTOMREQUEST => $method,
            CURLOPT_HTTPHEADER => $flat,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_HEADER => true,
            CURLOPT_TIMEOUT => $this->timeout,
            CURLOPT_POSTFIELDS => $body ?? '',
        ]);
        $raw = curl_exec($ch);
        if ($raw === false) {
            $err = curl_error($ch);
            curl_close($ch);
            throw new ApiConnectionError($err);
        }
        $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        $headerSize = (int) curl_getinfo($ch, CURLINFO_HEADER_SIZE);
        curl_close($ch);

        $rawHeaders = substr((string) $raw, 0, $headerSize);
        $bodyStr = substr((string) $raw, $headerSize);
        $parsed = [];
        foreach (explode("\r\n", $rawHeaders) as $line) {
            $i = strpos($line, ':');
            if ($i !== false) {
                $parsed[strtolower(trim(substr($line, 0, $i)))] = trim(substr($line, $i + 1));
            }
        }
        return [$status, $bodyStr, $parsed];
    }

    public static function uuid4(): string
    {
        $b = random_bytes(16);
        $b[6] = chr((ord($b[6]) & 0x0f) | 0x40);
        $b[8] = chr((ord($b[8]) & 0x3f) | 0x80);
        return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($b), 4));
    }

    private static function backoff(int $attempt): float
    {
        return min(0.5 * (2 ** ($attempt - 1)), 8.0);
    }

    /** @param array<string,string> $headers */
    private static function retryDelay(array $headers, int $attempt): float
    {
        $ra = $headers['retry-after'] ?? null;
        if ($ra !== null && ctype_digit((string) $ra)) {
            return (float) $ra;
        }
        return self::backoff($attempt);
    }

    private static function sleep(float $seconds): void
    {
        if ($seconds > 0) {
            usleep((int) ($seconds * 1_000_000));
        }
    }
}
