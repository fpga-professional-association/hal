#!/usr/bin/env python3
import argparse
import sys
import os.path
from os import path
import re
from enum import Enum
import datetime
import copy

PROGRAM = os.path.basename(__file__)


def fail(message, status=2):
    """Print an error naming what went wrong and leave with a nonzero status.

    Issue #67: ``changelog_converter.py garbage-input`` used to read the word
    ``garbage-input`` as the *contents* of a changelog, find no release entry in
    it, print "Skipping write out. No output produced!" and exit 0.  A packaging
    script that shells out to this converter cannot tell that apart from a
    successful conversion, so a missing or unusable input has to be an error.
    """
    print("{}: error: {}".format(PROGRAM, message), file=sys.stderr)
    sys.exit(status)


def read_input(args):
    """The changelog text to convert, or a nonzero exit naming the missing path."""
    if args.input_file:
        if not path.isfile(args.input_file):
            fail("input file does not exist: {}".format(args.input_file))
        with open(args.input_file, 'r') as f:
            return f.read()

    if not args.input:
        fail(
            "no input: give the changelog contents as the positional argument, "
            "or a file with -i/--input-file"
        )

    # The positional argument is documented as the changelog *contents*, and it
    # is routinely handed a filename instead.  A changelog is never a single
    # line, so a one-line positional is a path: read it if it exists, and name
    # it if it does not, rather than converting it to nothing and exiting 0.
    if "\n" not in args.input.strip():
        if path.isfile(args.input):
            with open(args.input, 'r') as f:
                return f.read()
        fail("input file does not exist: {}".format(args.input))

    return args.input

class release_info:

    def __init__(self):
        self.major = int()
        self.minor = int()
        self.patch = int()
        self.release = str()
        self.urgency = 'medium'
        self.description = []
        self.date = str()
        self.author = "Sebastian Wallat"
        self.email = "sebastian.wallat@rub.de"

    def __str__(self):
        return "entry: version: v{}.{}.{} release: {} urgency: {} date: {} author: {} email: {} description {}".format(self.major, self.minor, self.patch, self.release, self.urgency, self.date, self.author, self.email, self.description)

def parse_markdown(input, release = 'bionic'):
    token = [i.strip() for i in input.split("\n")]
    regex_start = r"\#\#\s+\[(?P<major>\d+).(?P<minor>\d+).(?P<patch>\d+)\]\s+-\s+(?P<date>(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) (\d{2}):(\d{2}):(\d{2})(\+|\-)(\d{2}):(\d{2}))\s*(\(urgency: (?P<urgency>low|medium|high)\))?"
    regex_hyperlink_section = r"\[//\]:\s+#\s+\(Hyperlink section\)"
    entries = []
    current_entry = None
    in_hyperlink_section = False

    for line in token:
        match_start = re.search(regex_start, line)
        match_hyperlink = re.search(regex_hyperlink_section, line)
        if match_start:
            if current_entry:
                entries.append(current_entry)
                current_entry = None
            current_entry = release_info()
            current_entry.major = match_start.group('major')
            current_entry.minor = match_start.group('minor')
            current_entry.patch = match_start.group('patch')
            current_entry.release = release
            current_entry.urgency = match_start.group('urgency')
            date_str = match_start.group('date')
            from dateutil import parser
            current_entry.date = parser.parse(date_str)

        elif match_hyperlink:
            in_hyperlink_section = True
            entries.append(current_entry)
            current_entry = None
        else:
            if current_entry != None and not in_hyperlink_section:
                current_entry.description.append(line)
    return entries

def to_debian(input, release = 'bionic', for_ppa_debian_dir = False, ppa_version = "ppa1"):
    entries = parse_markdown(input, release)

    if for_ppa_debian_dir and not entries:
        fail("no release entry found in the input; cannot build a PPA debian/changelog", status=1)

    if for_ppa_debian_dir:
        last_entry = entries[0]
        new_entry = copy.deepcopy(last_entry)
        new_entry.patch = "{}-{}~{}1".format(last_entry.patch, ppa_version, release)
        entries.insert(0, new_entry)
    # Write output
    output_lines = []
    for entry in entries:
        if output_lines:
            output_lines.append("")
        output_lines.append("hal-reverse ({}.{}.{}) {}; urgency={}".format(entry.major, entry.minor, entry.patch, entry.release, entry.urgency))
        output_lines.append("")

        #Strip empty lines at begin and end of description
        if entry.description and entry.description[0] == "":
            entry.description.remove(entry.description[0])
        if entry.description and entry.description[-1] == "":
            entry.description.pop()

        from email import utils
        import time
        timestmp = time.mktime(entry.date.timetuple())
        output_lines += entry.description
        output_lines.append("")
        output_lines.append(" -- {} <{}> {}".format(entry.author, entry.email,utils.formatdate(timestmp)))

    return "\n".join(output_lines)

def print_last_entry_info(input, release = 'bionic'):
    entries = parse_markdown(input, release)
    if not entries:
        fail("no release entry found in the input; nothing to report", status=1)
    ret_val = []
    entry = entries[0]
    ret_val.append("CHANGELOG_LAST_VERSION: {}.{}.{}".format(entry.major, entry.minor, entry.patch))
    ret_val.append("CHANGELOG_LAST_VERSION_MAJOR: {}".format(entry.major))
    ret_val.append("CHANGELOG_LAST_VERSION_MINOR: {}".format(entry.minor))
    ret_val.append("CHANGELOG_LAST_VERSION_PATCH: {}".format(entry.patch))
    ret_val.append("CHANGELOG_LAST_MESSAGE:")
    for line in entry.description:
        ret_val.append(line)
    print("\n".join(ret_val))

class parse_debian_states(Enum):
    read_start = 1
    read_end = 2


def to_markdown(input):
    token = [i.strip() for i in input.split("\n")]
    regex_start = r"hal-reverse\s+\((?P<major>\d+).(?P<minor>\d+).(?P<patch>\d+)\)\s+(?P<release>[\w]+);\s+urgency=(?P<urgency>[\w]+)"
    regex_end = r"\-\-\s+(?P<author>[\w ]+)\s+\<(?P<email>[\w.@]+)\>\s+(?P<date>(?P<day_of_week>Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(?P<day>\d{2})\s+(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(?P<year>\d{4})\s+(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+(?P<time_shisht>(\+|\-)?\d{4}))"

    entries = []
    current_entry = None
    state = parse_debian_states.read_start
    for line in token:
        if state == parse_debian_states.read_start:
            match = re.search(regex_start, line)
            if match:
                current_entry = release_info()
                current_entry.major = match.group('major')
                current_entry.minor = match.group('minor')
                current_entry.patch = match.group('patch')
                current_entry.release = match.group('release')
                current_entry.urgency = match.group('urgency')
                state = parse_debian_states.read_end
        elif state == parse_debian_states.read_end:
            match = re.search(regex_end, line)
            if match:
                current_entry.author = match.group('author')
                current_entry.email = match.group('email')

                # Parse Timestamp
                date_str = match.group('date')
                from dateutil import parser
                current_entry.date = parser.parse(date_str)

                entries.append(current_entry)
                current_entry = None
                state = parse_debian_states.read_start
            else:
                if line != '':
                    current_entry.description.append(line)
    # Write output
    output_lines = []
    for entry in entries:
        if output_lines:
            output_lines.append("")
        line = "## [{}.{}.{}] - {}".format(entry.major, entry.minor, entry.patch, entry.date.isoformat(' '))

        line = "{} (urgency: {})".format(line, entry.urgency)
        output_lines.append(line)
        output_lines.append("")
        output_lines += entry.description

    return "\n".join(output_lines)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert CHANGELOG from Markdown to Debian Syntax and vice versa.')
    parser.add_argument('input', type=str, nargs='?', help='Content of file to convert')
    parser.add_argument('-i', '--input-file', type=str, default='', help='Path to input file')
    parser.add_argument('--to', choices=['markdown', 'debian'], default='debian', help='Specify the desired format')
    parser.add_argument('-o', '--output-file', type=str, default="", help='Specify the output file name')
    parser.add_argument('--verbose', '-v', action='count')
    parser.add_argument('--force-write', '-f', action='store_true', default=False, help="Overwrite exiting file?")
    parser.add_argument('-r', '--release', type=str, default='bionic', help="Specify Ubuntu release to use!")
    parser.add_argument('--last-message', action='store_true', help="Print last entry info")
    parser.add_argument('--ppa-version', type=str, default='ppa1', help="The ppa version for upload to ubuntu")
    parser.add_argument('--for-ppa-debian-dir', action='store_true')
    parser.add_argument('-p', '--just-print', action='store_true', help="Just print! Do not write to file!")
    args = parser.parse_args()
    input = read_input(args)

    result = ""
    if args.last_message:
        print_last_entry_info(input, args.release)
        exit(0)
        
    if args.to == 'markdown':
        result = to_markdown(input)
    elif args.to == 'debian':
        result = to_debian(input, args.release, args.for_ppa_debian_dir, args.ppa_version)

    if not result:
        fail(
            "no release entry found in {}; nothing to convert to {}".format(
                args.input_file if args.input_file else "the given input", args.to
            ),
            status=1,
        )

    if args.just_print:
        print(result)
        exit(0)

    output_filename = args.output_file
    if output_filename != '':
        if path.exists(output_filename) and not args.force_write:
            print("Cannot overwrite existing file!", file=sys.stderr)
            exit(-1)

    if output_filename != '':
        with open(output_filename, 'w+') as f:
            f.write(result)
    else:
        print(result)




