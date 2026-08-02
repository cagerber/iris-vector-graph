from dataclasses import dataclass
from typing import Optional, List
import enum


class TokenType(enum.Enum):
    # Keywords
    MATCH = "MATCH"
    WITH = "WITH"
    WHERE = "WHERE"
    RETURN = "RETURN"
    ORDER = "ORDER"
    BY = "BY"
    LIMIT = "LIMIT"
    SKIP = "SKIP"
    ASC = "ASC"
    DESC = "DESC"
    DISTINCT = "DISTINCT"
    AS = "AS"
    AND = "AND"
    OR = "OR"
    XOR = "XOR"
    NOT = "NOT"
    TRUE = "TRUE"
    FALSE = "FALSE"
    NULL = "NULL"
    IN = "IN"
    IS = "IS"
    STARTS = "STARTS"
    WITH_KW = "WITH_KW"
    CONTAINS = "CONTAINS"
    ENDS = "ENDS"
    UNWIND = "UNWIND"
    CASE = "CASE"
    WHEN = "WHEN"
    THEN = "THEN"
    ELSE = "ELSE"
    END = "END"
    UNION = "UNION"
    ALL = "ALL"
    CREATE = "CREATE"
    MERGE = "MERGE"
    DELETE = "DELETE"
    SET = "SET"
    REMOVE = "REMOVE"
    ON = "ON"
    DETACH = "DETACH"
    CALL = "CALL"
    YIELD = "YIELD"
    TRANSACTIONS = "TRANSACTIONS"
    ROWS = "ROWS"

    # Literals and Identifiers
    IDENTIFIER = "IDENTIFIER"
    STRING_LITERAL = "STRING_LITERAL"
    INTEGER_LITERAL = "INTEGER_LITERAL"
    FLOAT_LITERAL = "FLOAT_LITERAL"
    PARAMETER = "PARAMETER"

    # Operators and Punctuation
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    LBRACE = "{"
    RBRACE = "}"
    COMMA = ","
    DOT = "."
    COLON = ":"
    PIPE = "|"
    EQUALS = "="
    NOT_EQUALS = "<>"
    LESS_THAN = "<"
    LESS_THAN_OR_EQUAL = "<="
    GREATER_THAN = ">"
    GREATER_THAN_OR_EQUAL = ">="
    PLUS = "+"
    PLUS_EQUAL = "+="
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    TILDE = "~"
    REGEX_MATCH = "=~"
    PERCENT = "%"
    CARET = "^"
    FOREACH = "FOREACH"
    ARROW_LEFT = "<-"
    ARROW_RIGHT = "->"

    EOF = "EOF"


@dataclass(slots=True, frozen=True)
class Token:
    kind: TokenType
    value: Optional[str] = None
    pos: int = 0
    line: int = 1
    column: int = 1


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.cursor = 0
        self.line = 1
        self.column = 1
        self.tokens: List[Token] = []
        self._tokenize()
        self.token_index = 0

    def _tokenize(self):
        while self.cursor < len(self.source):
            char = self.source[self.cursor]

            if char.isspace():
                self._skip_whitespace()
                continue

            start_pos = self.cursor
            start_col = self.column

            match char:
                case "(":
                    self._add_token(TokenType.LPAREN, char)
                case ")":
                    self._add_token(TokenType.RPAREN, char)
                case "[":
                    self._add_token(TokenType.LBRACKET, char)
                case "]":
                    self._add_token(TokenType.RBRACKET, char)
                case "{":
                    self._add_token(TokenType.LBRACE, char)
                case "}":
                    self._add_token(TokenType.RBRACE, char)
                case ",":
                    self._add_token(TokenType.COMMA, char)
                case ".":
                    # Check if this is a float literal like .1 (digit follows)
                    if self._peek() is not None and self._peek().isdigit():
                        self._tokenize_number()
                    else:
                        self._add_token(TokenType.DOT, char)
                case ":":
                    self._add_token(TokenType.COLON, char)
                case "|":
                    self._add_token(TokenType.PIPE, char)
                case "+":
                    if self._peek() == "=":
                        self.tokens.append(Token(TokenType.PLUS_EQUAL, "+=", self.cursor, self.line, self.column))
                        self.cursor += 2
                        self.column += 2
                    else:
                        self._add_token(TokenType.PLUS, char)
                case "*":
                    self._add_token(TokenType.STAR, char)
                case "/":
                    self._add_token(TokenType.SLASH, char)
                case "%":
                    self._add_token(TokenType.PERCENT, char)
                case "^":
                    self._add_token(TokenType.CARET, char)
                case "=":
                    if self._peek() == "~":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.REGEX_MATCH, "=~")
                    else:
                        self._add_token(TokenType.EQUALS, char)
                case "<":
                    if self._peek() == ">":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.NOT_EQUALS, "<>")
                    elif self._peek() == "=":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.LESS_THAN_OR_EQUAL, "<=")
                    elif self._peek() == "-":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.ARROW_LEFT, "<-")
                    else:
                        self._add_token(TokenType.LESS_THAN, char)
                case ">":
                    if self._peek() == "=":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.GREATER_THAN_OR_EQUAL, ">=")
                    else:
                        self._add_token(TokenType.GREATER_THAN, char)
                case "-":
                    if self._peek() == "[":
                        self._add_token(TokenType.MINUS, char)
                    elif self._peek() == ">":
                        self.cursor += 1
                        self.column += 1
                        self._add_token(TokenType.ARROW_RIGHT, "->")
                    else:
                        self._add_token(TokenType.MINUS, char)
                case '"' | "'":
                    self._tokenize_string(char)
                case '`':
                    self._tokenize_backtick_identifier()
                case "$":
                    self._tokenize_parameter()
                case c if c.isdigit():
                    self._tokenize_number()
                case c if c.isalpha() or c == "_":
                    self._tokenize_identifier_or_keyword()
                case _:
                    raise SyntaxError(
                        f"Unexpected character '{char}' at line {self.line}, col {self.column}"
                    )

        self.tokens.append(
            Token(TokenType.EOF, pos=self.cursor, line=self.line, column=self.column)
        )

    def _add_token(self, kind: TokenType, value: str):
        self.tokens.append(Token(kind, value, self.cursor, self.line, self.column))
        self.cursor += 1
        self.column += 1

    def _peek(self) -> Optional[str]:
        if self.cursor + 1 < len(self.source):
            return self.source[self.cursor + 1]
        return None

    def _skip_whitespace(self):
        while self.cursor < len(self.source) and self.source[self.cursor].isspace():
            if self.source[self.cursor] == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.cursor += 1

    def _tokenize_backtick_identifier(self):
        start_pos = self.cursor
        start_col = self.column
        self.cursor += 1
        self.column += 1
        value = ""
        while self.cursor < len(self.source) and self.source[self.cursor] != '`':
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
        if self.cursor >= len(self.source):
            raise SyntaxError(
                f"Unterminated backtick identifier at line {self.line}, col {start_col}"
            )
        self.cursor += 1
        self.column += 1
        self.tokens.append(
            Token(TokenType.IDENTIFIER, value, start_pos, self.line, start_col)
        )

    def _tokenize_string(self, quote: str):
        start_pos = self.cursor
        start_col = self.column
        self.cursor += 1
        self.column += 1
        value = ""
        while self.cursor < len(self.source) and self.source[self.cursor] != quote:
            if self.source[self.cursor] == "\\":
                self.cursor += 1
                self.column += 1
                if self.cursor < len(self.source):
                    value += self.source[self.cursor]
            else:
                value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1

        if self.cursor >= len(self.source):
            raise SyntaxError(
                f"Unterminated string starting at line {self.line}, col {start_col}"
            )

        self.cursor += 1
        self.column += 1
        self.tokens.append(
            Token(TokenType.STRING_LITERAL, value, start_pos, self.line, start_col)
        )

    def _tokenize_parameter(self):
        start_pos = self.cursor
        start_col = self.column
        self.cursor += 1
        self.column += 1
        value = ""
        while self.cursor < len(self.source) and (
            self.source[self.cursor].isalnum() or self.source[self.cursor] == "_"
        ):
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
        self.tokens.append(
            Token(TokenType.PARAMETER, value, start_pos, self.line, start_col)
        )

    def _tokenize_number(self):
        start_pos = self.cursor
        start_col = self.column
        value = ""
        is_float = False

        # Handle leading-dot float literal (.1, .5e2, etc.)
        if self.cursor < len(self.source) and self.source[self.cursor] == ".":
            is_float = True
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
            # Consume remaining digits
            while self.cursor < len(self.source) and self.source[self.cursor].isdigit():
                value += self.source[self.cursor]
                self.cursor += 1
                self.column += 1
            # Exponent
            if (
                self.cursor < len(self.source)
                and self.source[self.cursor] in ("e", "E")
            ):
                value += self.source[self.cursor]
                self.cursor += 1
                self.column += 1
                if self.cursor < len(self.source) and self.source[self.cursor] in ("+", "-"):
                    value += self.source[self.cursor]
                    self.cursor += 1
                    self.column += 1
                while self.cursor < len(self.source) and self.source[self.cursor].isdigit():
                    value += self.source[self.cursor]
                    self.cursor += 1
                    self.column += 1
            self.tokens.append(Token(TokenType.FLOAT_LITERAL, value, start_pos, self.line, start_col))
            return

        # Consume leading digits
        while self.cursor < len(self.source) and self.source[self.cursor].isdigit():
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1

        # Check for hex (0x...) or octal (0o...) integer prefix
        if value == "0" and self.cursor < len(self.source):
            next_ch = self.source[self.cursor]
            if next_ch in ("x", "X"):
                # Hex literal: consume hex digits
                self.cursor += 1
                self.column += 1
                hex_digits = ""
                while self.cursor < len(self.source) and (
                    self.source[self.cursor] in "0123456789abcdefABCDEF"
                ):
                    hex_digits += self.source[self.cursor]
                    self.cursor += 1
                    self.column += 1
                decimal_value = str(int(hex_digits, 16)) if hex_digits else "0"
                self.tokens.append(Token(TokenType.INTEGER_LITERAL, decimal_value, start_pos, self.line, start_col))
                return
            elif next_ch in ("o", "O"):
                # Octal literal: consume octal digits
                self.cursor += 1
                self.column += 1
                oct_digits = ""
                while self.cursor < len(self.source) and self.source[self.cursor] in "01234567":
                    oct_digits += self.source[self.cursor]
                    self.cursor += 1
                    self.column += 1
                decimal_value = str(int(oct_digits, 8)) if oct_digits else "0"
                self.tokens.append(Token(TokenType.INTEGER_LITERAL, decimal_value, start_pos, self.line, start_col))
                return

        # Check for fractional part
        if (
            self.cursor < len(self.source)
            and self.source[self.cursor] == "."
            and not (
                self.cursor + 1 < len(self.source)
                and self.source[self.cursor + 1] == "."
            )
        ):
            is_float = True
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
            while self.cursor < len(self.source) and self.source[self.cursor].isdigit():
                value += self.source[self.cursor]
                self.cursor += 1
                self.column += 1

        # Check for exponent (e/E with optional sign) — applies to both int and float
        if (
            self.cursor < len(self.source)
            and self.source[self.cursor] in ("e", "E")
        ):
            is_float = True
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
            if self.cursor < len(self.source) and self.source[self.cursor] in ("+", "-"):
                value += self.source[self.cursor]
                self.cursor += 1
                self.column += 1
            while self.cursor < len(self.source) and self.source[self.cursor].isdigit():
                value += self.source[self.cursor]
                self.cursor += 1
                self.column += 1

        kind = TokenType.FLOAT_LITERAL if is_float else TokenType.INTEGER_LITERAL
        self.tokens.append(Token(kind, value, start_pos, self.line, start_col))

    def _tokenize_identifier_or_keyword(self):
        start_pos = self.cursor
        start_col = self.column
        value = ""
        while self.cursor < len(self.source) and (
            self.source[self.cursor].isalnum() or self.source[self.cursor] == "_"
        ):
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1

        upper_value = value.upper()
        try:
            # Check for STARTS WITH
            if upper_value == "STARTS" and self._peek_keyword("WITH"):
                self.tokens.append(
                    Token(TokenType.STARTS, value, start_pos, self.line, start_col)
                )
                self._consume_keyword("WITH", TokenType.WITH_KW)
                return

            # Check for ENDS WITH
            if upper_value == "ENDS" and self._peek_keyword("WITH"):
                self.tokens.append(
                    Token(TokenType.ENDS, value, start_pos, self.line, start_col)
                )
                self._consume_keyword("WITH", TokenType.WITH_KW)
                return

            kind = TokenType[upper_value]
            self.tokens.append(Token(kind, value, start_pos, self.line, start_col))
        except KeyError:
            self.tokens.append(
                Token(TokenType.IDENTIFIER, value, start_pos, self.line, start_col)
            )

    def _peek_keyword(self, keyword: str) -> bool:
        # Simple peek for multi-word keywords
        current_cursor = self.cursor

        # Skip whitespace
        while (
            current_cursor < len(self.source) and self.source[current_cursor].isspace()
        ):
            current_cursor += 1

        k_val = ""
        while (
            current_cursor < len(self.source) and self.source[current_cursor].isalpha()
        ):
            k_val += self.source[current_cursor]
            current_cursor += 1

        return k_val.upper() == keyword.upper()

    def _consume_keyword(self, keyword: str, kind: TokenType):
        self._skip_whitespace()
        start_pos = self.cursor
        start_col = self.column
        value = ""
        while self.cursor < len(self.source) and self.source[self.cursor].isalpha():
            value += self.source[self.cursor]
            self.cursor += 1
            self.column += 1
        self.tokens.append(Token(kind, value, start_pos, self.line, start_col))

    def peek(self) -> Token:
        if self.token_index < len(self.tokens):
            return self.tokens[self.token_index]
        return self.tokens[-1]

    def peek_ahead(self, offset: int) -> Token:
        idx = self.token_index + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return self.tokens[-1]

    def eat(self) -> Token:
        tok = self.peek()
        if tok.kind != TokenType.EOF:
            self.token_index += 1
        return tok
