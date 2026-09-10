<?php

declare(strict_types=1);

namespace Akilent;

/** Base class for every error raised by the SDK. */
class AkilentError extends \Exception
{
}

/** The request never reached the API (DNS, TLS, timeout, connection reset). */
class ApiConnectionError extends AkilentError
{
}

/** An inbound webhook signature failed verification. */
class SignatureVerificationError extends AkilentError
{
}

/** The API returned a non-2xx response. */
class ApiStatusError extends AkilentError
{
    public int $statusCode;
    public ?string $errorCode;
    public ?string $requestId;
    public ?string $docsUrl;
    /** @var mixed */
    public $body;
    public ?int $retryAfter = null;

    public function __construct(
        string $message,
        int $statusCode,
        ?string $errorCode = null,
        ?string $requestId = null,
        ?string $docsUrl = null,
        $body = null
    ) {
        parent::__construct($message);
        $this->statusCode = $statusCode;
        $this->errorCode = $errorCode;
        $this->requestId = $requestId;
        $this->docsUrl = $docsUrl;
        $this->body = $body;
    }
}

class ValidationError extends ApiStatusError
{
}

class AuthenticationError extends ApiStatusError
{
}

class PermissionDeniedError extends ApiStatusError
{
}

class NotFoundError extends ApiStatusError
{
}

class ConflictError extends ApiStatusError
{
}

class RateLimitError extends ApiStatusError
{
}

class ServerError extends ApiStatusError
{
}

final class ErrorFactory
{
    /**
     * @param array<string,string> $headers lower-cased header map
     * @param mixed $body
     */
    public static function fromResponse(int $status, $body, array $headers): ApiStatusError
    {
        $err = (is_array($body) && isset($body['error']) && is_array($body['error'])) ? $body['error'] : [];
        $message = $err['message'] ?? ('HTTP ' . $status);
        $errorCode = $err["code"] ?? null;
        $requestId = $err['request_id'] ?? ($headers['x-request-id'] ?? null);
        $docsUrl = $err['docs_url'] ?? null;

        $cls = match ($status) {
            400 => ValidationError::class,
            401 => AuthenticationError::class,
            403 => PermissionDeniedError::class,
            404 => NotFoundError::class,
            409 => ConflictError::class,
            429 => RateLimitError::class,
            default => $status >= 500 ? ServerError::class : ApiStatusError::class,
        };

        /** @var ApiStatusError $e */
        $e = new $cls($message, $status, $errorCode, $requestId, $docsUrl, $body);
        if ($e instanceof RateLimitError) {
            $ra = $headers['retry-after'] ?? null;
            $e->retryAfter = ($ra !== null && ctype_digit((string) $ra)) ? (int) $ra : null;
        }
        return $e;
    }
}
