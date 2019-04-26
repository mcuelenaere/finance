import pygal
import sys
from collections import defaultdict
from datetime import timedelta
from itertools import groupby
from finance.config import parse_config
from pygal.style import DefaultStyle


def sort_and_group(data, key):
    data = sorted(data, key=key)
    return groupby(data, key)


def render_stacked_chart(transactions, time_format: str, title: str):
    # group by time unit
    x_labels = []
    series = defaultdict(dict)
    for time_unit, batch in sort_and_group(transactions, key=lambda t: t.timestamp.strftime(time_format)):
        x_labels.append(time_unit)
        for category, batch in sort_and_group(batch, key=lambda t: categorizer.categorize(t)):
            series[category][time_unit] = sum(t.amount for t in batch)

    # generate graph
    graph = pygal.StackedBar(
        value_formatter=lambda x: f'€ {x}',
        width=1200,
        height=600,
        margin=50,
        legend_at_bottom=True,
        tooltip_border_radius=3,
        style=DefaultStyle(tooltip_font_size=8)
    )
    graph.x_labels = x_labels
    graph.title = title
    for category, time_units in series.items():
        graph.add(category, [time_units.get(x, None) for x in x_labels])
    return graph

if __name__ == "__main__":
    config = parse_config(sys.argv[1])
    categorizer = config.categorizer()
    transactions = sorted(config.transactions(), key=lambda t: (t.timestamp, t.source.number))
    last_transaction_time = max(t.timestamp for t in transactions)

    six_months_ago = last_transaction_time - timedelta(weeks=4 * 6)
    render_stacked_chart(
        [t for t in transactions if t.timestamp > six_months_ago],
        time_format='%Y-%W',
        title='Transactions per week'
    ).render_to_file('per_week.svg')

    two_years_ago = last_transaction_time - timedelta(weeks=4 * 12 * 2)
    render_stacked_chart(
        [t for t in transactions if t.timestamp > two_years_ago],
        time_format='%Y-%m',
        title='Transactions per month'
    ).render_to_file('per_month.svg')

    render_stacked_chart(transactions, '%Y', title='Transactions per year').render_to_file('per_year.svg')
