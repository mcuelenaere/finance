from .belfius_be import parse_file as parse_belfius_file
from .ing_be import parse_file as parse_ing_file

formats = {
    'belfius_be': parse_belfius_file,
    'ing_be': parse_ing_file,
}
