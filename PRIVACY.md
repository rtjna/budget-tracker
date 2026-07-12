# Privacy Notice

**Application:** Personal budget tracker
**Contact for data protection matters:** rtjonameeuw@gmail.com

This is a personal, non-commercial application operated by a single individual
for their own use. It has no other users.

## What data is processed

The application retrieves account information (account details, balances, and
transactions) via bank APIs and statement files, exclusively from bank accounts
owned by the operator, with the operator's explicit consent.

## How data is used and stored

- Data is used solely for personal budgeting and spending analysis.
- Data is stored locally on hardware owned and controlled by the operator.
- No analytics, advertising, or tracking of any kind is performed.

## Third-party processing

- **Anthropic (optional, operator-initiated):** when the operator uses the
  "Ask Claude" categorization feature, the following is sent to the
  Anthropic API: normalized merchant names, one sample transaction
  description per merchant, the operator's category list, and examples of
  the operator's own merchant-to-category decisions. Amounts, dates,
  balances, account identifiers, and personal financial totals are never
  transmitted. This processing only occurs when explicitly triggered.
- **Monzo and Splitwise (operator-initiated):** the application retrieves
  the operator's own data from these services via their APIs under the
  operator's own credentials. Nothing is written back to them beyond the
  API calls needed to read that data.
- No other data leaves the operator's hardware.

## Data subject rights

The sole data subject is the operator. Bank consents can be revoked at any
time through the connected bank or by deleting the authorization, and all
stored data can be deleted by the operator at will.
