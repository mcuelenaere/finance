import csv
import sys
from finance.config import parse_config

if __name__ == "__main__":
    config = parse_config(sys.argv[1])
    categorizer = config.categorizer()
    transactions = sorted(config.transactions(), key=lambda t: (t.timestamp, t.source.number))
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            'source.number', 'source.name', 'destination.number', 'destination.name', 'amount', 'currency',
            'date', 'category', 'description'
        ],
        dialect='unix'
    )
    writer.writeheader()
    for transaction in transactions:
        writer.writerow({
            'source.number': transaction.source.number,
            'source.name': transaction.source.name,
            'destination.number': transaction.destination.number,
            'destination.name': transaction.destination.name,
            'amount': transaction.amount,
            'currency': transaction.currency.value,
            'date': transaction.timestamp,
            'category': categorizer.categorize(transaction),
            'description': transaction.description
        })
