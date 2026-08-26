from .belfius_be import parse_file as parse_belfius_file
from .coinbase_pro import parse_file as parse_coinbasepro_file
from .degiro import parse_file as parse_degiro_file
from .ing_be import parse_file as parse_ing_file

formats = {
    'belfius_be': parse_belfius_file,
    'coinbase_pro': parse_coinbasepro_file,
    'degiro': parse_degiro_file,
    'ing_be': parse_ing_file,
}
