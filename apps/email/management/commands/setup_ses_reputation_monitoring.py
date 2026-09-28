"""Management command to verify and document SES reputation monitoring setup.

This command helps operators understand and validate the configuration needed
to monitor SES sending reputation via CloudWatch and SNS event destinations.
"""

import logging

import boto3
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Setup helper for SES reputation monitoring."""

    help = "Verify and setup SES reputation monitoring with CloudWatch/SNS event destinations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--configuration-set",
            type=str,
            required=True,
            help="SES configuration set name to check/setup",
        )
        parser.add_argument(
            "--region",
            type=str,
            default="us-east-1",
            help="AWS region (default: us-east-1)",
        )
        parser.add_argument(
            "--sns-topic-arn",
            type=str,
            help="SNS topic ARN for bounce/complaint notifications (for reference only; create manually)",
        )

    def handle(self, *args, **options):
        config_set = options["configuration_set"]
        region = options["region"]
        sns_arn = options.get("sns_topic_arn")

        try:
            client = boto3.client("sesv2", region_name=region)
        except Exception as e:
            raise CommandError(f"Failed to connect to SES in {region}: {e}")

        self.stdout.write("\n=== SES Reputation Monitoring Setup ===\n")
        self.stdout.write(f"Configuration Set: {config_set}")
        self.stdout.write(f"Region: {region}\n")

        try:
            client.get_configuration_set(ConfigurationSetName=config_set)
            self.stdout.write(
                self.style.SUCCESS(f"[OK] Configuration set '{config_set}' exists\n")
            )
        except client.exceptions.NotFoundException:
            raise CommandError(
                f"Configuration set '{config_set}' not found in {region}. "
                "Create it first: aws sesv2 create-configuration-set --configuration-set-name <name>"
            )

        self._check_event_destinations(client, config_set)
        self._print_setup_instructions(config_set, region, sns_arn)

    def _check_event_destinations(self, client, config_set: str):
        """Check which event destinations are currently configured."""
        self.stdout.write("Event Destinations:\n")

        try:
            response = client.list_event_destinations()
            destinations = response.get("EventDestinations", [])

            if not destinations:
                self.stdout.write(
                    self.style.WARNING(
                        "  [WARN] No event destinations found. "
                        "Setup is needed for reputation monitoring.\n"
                    )
                )
                return

            for dest in destinations:
                name = dest.get("Name", "")
                dest.get("EventDestinationName", "")
                enabled = dest.get("Enabled", False)
                status = (
                    self.style.SUCCESS("enabled")
                    if enabled
                    else self.style.ERROR("disabled")
                )
                self.stdout.write(f"  • {name}: {status}\n")

                event_types = dest.get("MatchingEventTypes", [])
                if event_types:
                    self.stdout.write(f"    Event types: {', '.join(event_types)}\n")

        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f"  [WARN] Could not list event destinations: {e}\n")
            )

    def _print_setup_instructions(
        self, config_set: str, region: str, sns_arn: str | None
    ):
        """Print instructions for setting up event destinations."""
        self.stdout.write("\nSetup Instructions:\n")
        self.stdout.write(
            """
1. Create SNS Topic (if not already created):
   aws sns create-topic --name ses-bounces-complaints

2. Note the TopicArn from the response, then create event destinations:

   # For bounce notifications:
   aws sesv2 put-event-destination \\
     --configuration-set-name """
            + config_set
            + """ \\
     --event-destination-name "BounceNotification" \\
     --matching-event-types "BOUNCE" \\
     --enabled \\
     --event-destination-definition "SnsDestination={TopicArn=arn:aws:sns:"""
            + region
            + """:ACCOUNT_ID:ses-bounces-complaints}"

   # For complaint notifications:
   aws sesv2 put-event-destination \\
     --configuration-set-name """
            + config_set
            + """ \\
     --event-destination-name "ComplaintNotification" \\
     --matching-event-types "COMPLAINT" \\
     --enabled \\
     --event-destination-definition "SnsDestination={TopicArn=arn:aws:sns:"""
            + region
            + """:ACCOUNT_ID:ses-bounces-complaints}"

3. Update MailProviderSettings.ses_sns_topic_arn in Django admin:
   - Go to Mail Provider Settings singleton
   - Set sns_topic_arn to: arn:aws:sns:"""
            + region
            + """:ACCOUNT_ID:ses-bounces-complaints
   - This enables webhook validation in apps/email/ses_webhooks.py

4. Create an HTTPS SNS subscription pointing at this app's SES webhook:
     aws sns subscribe --topic-arn <TOPIC_ARN> \\
       --protocol https \\
       --notification-endpoint https://<YOUR_HOST>/email/webhooks/ses/
   The endpoint (apps/email/ses_webhooks.py) auto-confirms the subscription and
   then ingests Bounce / Complaint / Delivery notifications.

5. Verify reputation metrics in CloudWatch:
   - Metric name: "Send", "Bounce", "Complaint", "Delivery" (after first sends)
   - Namespace: "AWS/SES"

Reference: https://docs.aws.amazon.com/ses/latest/dg/using-configuration-sets.html
"""
        )

        if sns_arn:
            self.stdout.write(f"\nYour configured SNS topic: {sns_arn}\n")
