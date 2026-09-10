<?php

declare(strict_types=1);

namespace Akilent\Resources;

final class Messages extends Base
{
    /** @param array<string,mixed>|null $variables */
    public function send(
        string $from,
        string $to,
        string $subject = '',
        ?string $text = null,
        ?string $html = null,
        ?string $template = null,
        ?array $variables = null,
        ?string $locale = null,
        ?string $idempotencyKey = null
    ): array {
        $payload = ['from' => $from, 'to' => $to];
        if ($subject !== '') {
            $payload['subject'] = $subject;
        }
        if ($text !== null) {
            $payload['text'] = $text;
        }
        if ($html !== null) {
            $payload['html'] = $html;
        }
        if ($template !== null) {
            $payload['template'] = $template;
        }
        if ($variables !== null) {
            $payload['template_variables'] = $variables;
        }
        if ($locale !== null) {
            $payload['locale'] = $locale;
        }
        return $this->t->request('POST', '/api/v1/messages', null, $payload, $idempotencyKey);
    }

    /** @param array<string,mixed> $filters */
    public function list(array $filters = []): array
    {
        return $this->t->request('GET', '/api/v1/messages', $filters ?: null);
    }

    public function retrieve(string $messageId): array
    {
        return $this->t->request('GET', "/api/v1/messages/{$messageId}");
    }

    /** @return array<int,array<string,mixed>> */
    public function events(string $messageId): array
    {
        return $this->t->request('GET', "/api/v1/messages/{$messageId}/events")['data'] ?? [];
    }

    /**
     * Yield every message across pages.
     *
     * @param array<string,mixed> $filters
     * @return \Generator<int,array<string,mixed>>
     */
    public function iterate(array $filters = [], int $pageSize = 100): \Generator
    {
        $offset = 0;
        while (true) {
            $page = $this->list(['limit' => $pageSize, 'offset' => $offset] + $filters);
            $rows = $page['data'] ?? [];
            foreach ($rows as $row) {
                yield $row;
            }
            $offset += count($rows);
            if (!$rows || $offset >= ($page['total'] ?? 0)) {
                return;
            }
        }
    }
}
