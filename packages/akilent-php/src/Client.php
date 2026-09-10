<?php

declare(strict_types=1);

namespace Akilent;

use Akilent\Resources\Campaigns;
use Akilent\Resources\Contacts;
use Akilent\Resources\Events;
use Akilent\Resources\Messages;
use Akilent\Resources\RequestLogs;
use Akilent\Resources\Templates;
use Akilent\Resources\Workflows;

/**
 * Official PHP client for the Akilent email API.
 *
 *   $client = new \Akilent\Client('ak_live_…');
 *   $client->messages->send(from: 'billing@acme.com', to: 'user@example.com',
 *                           subject: 'Receipt', text: 'Thanks!');
 */
final class Client
{
    public Transport $transport;
    public Messages $messages;
    public Templates $templates;
    public Campaigns $campaigns;
    public Contacts $contacts;
    public Events $events;
    public Workflows $workflows;
    public RequestLogs $requestLogs;

    public function __construct(
        string $apiKey,
        string $baseUrl = Transport::DEFAULT_BASE_URL,
        int $timeout = 30,
        int $maxRetries = 2
    ) {
        $this->transport = new Transport($apiKey, $baseUrl, $timeout, $maxRetries);
        $this->messages = new Messages($this->transport);
        $this->templates = new Templates($this->transport);
        $this->campaigns = new Campaigns($this->transport);
        $this->contacts = new Contacts($this->transport);
        $this->events = new Events($this->transport);
        $this->workflows = new Workflows($this->transport);
        $this->requestLogs = new RequestLogs($this->transport);
    }
}
