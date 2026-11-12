"""Proceso de las herramientas exactas de TISMA Code. Lo lanza exactas.py dentro de bubblewrap (sin red, sistema y
proyecto de solo lectura, límites de memoria y CPU): lee {"herramienta", "args"} en JSON por stdin y escribe
{"ok", "resultado"} por stdout.

Nada de eval(): las expresiones se interpretan con un evaluador propio sobre el árbol sintáctico de Python, con
racionales exactos (fractions.Fraction) y precisión arbitraria (mpmath) solo donde hace falta. sympy (cuyo
parse_expr sí usa eval) recibe la entrada ya filtrada y corre igualmente dentro de la jaula.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
import sys
from decimal import Decimal, getcontext
from fractions import Fraction

MAX_BITS = 200_000          # resultados enteros de hasta ~60.000 cifras
MAX_DIGITOS = 1000


class Fallo(Exception):
    pass


def _opcional(nombre: str):
    try:
        return __import__(nombre)
    except ImportError:
        return None


def falta(paquete: str) -> Fallo:
    return Fallo(f"Esta herramienta necesita {paquete}, que todavía no está instalado. Viene con la base del sistema "
                 "(se descarga sola en cuanto hay Internet) o con el uso Ingeniería de la Bienvenida.")


# ---------------------------------------------------------------- números
def _num(x):
    """Normaliza: bool → int, Fraction entera → int."""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, Fraction) and x.denominator == 1:
        return x.numerator
    return x


def _check(x):
    if isinstance(x, int) and x.bit_length() > MAX_BITS:
        raise Fallo("El resultado es demasiado grande (más de 60.000 cifras)")
    if isinstance(x, Fraction) and (x.numerator.bit_length() > MAX_BITS or x.denominator.bit_length() > MAX_BITS):
        raise Fallo("El resultado exacto es demasiado grande")
    return x


class Aprox:
    """Número irracional calculado con `digitos` cifras (mpmath) o, sin mpmath, en doble precisión."""
    def __init__(self, valor, digitos: int):
        self.v, self.d = valor, digitos

    def __repr__(self):
        return f"Aprox({self.v})"


class Evaluador:
    """Aritmética exacta con racionales; funciones trascendentes con precisión pedida."""

    def __init__(self, digitos: int = 30, variables: dict | None = None, modo: str = "exacto"):
        self.dig = max(5, min(MAX_DIGITOS, int(digitos)))
        self.vars = dict(variables or {})
        self.modo = modo              # "exacto" o "float" (para métodos numéricos)
        self.mp = _opcional("mpmath")
        if self.mp:
            self.mp.mp.dps = self.dig + 5
        self.aprox = False

    # -- conversión
    def a_mp(self, x):
        if isinstance(x, Aprox):
            return x.v
        if self.mp:
            if isinstance(x, Fraction):
                return self.mp.mpf(x.numerator) / x.denominator
            return self.mp.mpf(x)
        return float(x)

    def real(self, x):
        self.aprox = True
        return Aprox(x, self.dig)

    def flotante(self, x) -> float:
        return float(x.v) if isinstance(x, Aprox) else float(x)

    # -- funciones
    def _f1(self, nombre):
        def f(x):
            if self.modo == "float":
                return getattr(math, nombre)(self.flotante(x))
            if self.mp:
                return self.real(getattr(self.mp, nombre)(self.a_mp(x)))
            return self.real(getattr(math, nombre)(self.flotante(x)))
        return f

    def raiz(self, x, n=2):
        n = _num(n)
        x = _num(x)
        if self.modo != "float" and isinstance(n, int) and n > 0 and isinstance(x, (int, Fraction)) and x >= 0:
            fx = Fraction(x)
            rn, rd = _iroot(fx.numerator, n), _iroot(fx.denominator, n)
            if rn is not None and rd is not None:
                return _num(Fraction(rn, rd))
        if self.modo == "float":
            return self.flotante(x) ** (1 / float(n))
        if self.mp:
            return self.real(self.mp.root(self.a_mp(x), int(n)) if isinstance(n, int) else
                             self.a_mp(x) ** (1 / self.a_mp(n)))
        return self.real(float(x) ** (1 / float(n)))

    def log(self, x, base=None):
        if base is not None:
            return self.div(self.log(x), self.log(base))
        if self.modo == "float":
            return math.log(self.flotante(x))
        return self.real(self.mp.log(self.a_mp(x))) if self.mp else self.real(math.log(self.flotante(x)))

    def funciones(self) -> dict:
        f = {n: self._f1(n) for n in ("sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "exp")}
        f.update({
            "sen": f["sin"], "sqrt": self.raiz, "raiz": self.raiz, "cbrt": lambda x: self.raiz(x, 3),
            "ln": self.log, "log": self.log, "log10": lambda x: self.log(x, 10), "log2": lambda x: self.log(x, 2),
            "atan2": lambda y, x: (math.atan2(self.flotante(y), self.flotante(x)) if self.modo == "float" else
                                   self.real(self.mp.atan2(self.a_mp(y), self.a_mp(x)) if self.mp else
                                             math.atan2(self.flotante(y), self.flotante(x)))),
            "abs": self.abs,
            "floor": lambda x: math.floor(x if not isinstance(x, Aprox) else x.v),
            "ceil": lambda x: math.ceil(x if not isinstance(x, Aprox) else x.v),
            "round": self.redondear, "redondear": self.redondear,
            "factorial": self.factorial, "comb": self.comb, "perm": self.perm,
            "gcd": lambda *a: math.gcd(*[self.entero(x) for x in a]), "mcd": lambda *a: math.gcd(*[self.entero(x) for x in a]),
            "lcm": lambda *a: math.lcm(*[self.entero(x) for x in a]), "mcm": lambda *a: math.lcm(*[self.entero(x) for x in a]),
            "min": lambda *a: min(a, key=self.clave), "max": lambda *a: max(a, key=self.clave),
            "deg": lambda x: self.mul(x, self.div(180, self.const("pi"))),
            "rad": lambda x: self.mul(x, self.div(self.const("pi"), 180)),
            "isprime": self.es_primo, "primo": self.es_primo, "factores": self.factores,
            "hypot": lambda *a: self.raiz(sum((self.mul(v, v) for v in a), 0)),
        })
        return f

    def clave(self, x):
        return self.flotante(x) if isinstance(x, Aprox) else x

    def entero(self, x) -> int:
        x = _num(x)
        if not isinstance(x, int):
            raise Fallo("Esta función necesita números enteros")
        return x

    def factorial(self, x):
        n = self.entero(x)
        if not 0 <= n <= 20000:
            raise Fallo("factorial: n debe estar entre 0 y 20000")
        return math.factorial(n)

    def comb(self, n, k):
        return math.comb(self.entero(n), self.entero(k))

    def perm(self, n, k=None):
        return math.perm(self.entero(n), None if k is None else self.entero(k))

    def es_primo(self, x) -> bool:
        n = self.entero(x)
        if n < 2:
            return False
        for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % p == 0:
                return n == p
        d, s = n - 1, 0
        while d % 2 == 0:
            d, s = d // 2, s + 1
        for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):   # determinista hasta 3,3·10^24
            x2 = pow(a, d, n)
            if x2 in (1, n - 1):
                continue
            for _ in range(s - 1):
                x2 = pow(x2, 2, n)
                if x2 == n - 1:
                    break
            else:
                return False
        return True

    def factores(self, x):
        n = self.entero(x)
        if n < 2:
            return str(n)
        out, p = [], 2
        while p * p <= n and p < 10_000_000:
            while n % p == 0:
                out.append(p)
                n //= p
            p += 1 if p == 2 else 2
        if n > 1:
            out.append(n if p * p > n or self.es_primo(n) else f"{n} (sin factorizar del todo)")
        return " · ".join(str(v) for v in out)

    def redondear(self, x, n=0):
        n = self.entero(n)
        if isinstance(x, Aprox):
            x = Fraction(str(x.v)) if self.mp else Fraction(x.v)
        q = Fraction(10) ** n
        return _num(Fraction(round(Fraction(x) * q)) / q)

    def abs(self, x):
        if isinstance(x, Aprox):
            return Aprox(abs(x.v), x.d)
        return abs(x)

    def const(self, nombre):
        if self.modo == "float":
            return {"pi": math.pi, "e": math.e, "tau": math.tau}[nombre]
        if self.mp:
            return self.real({"pi": self.mp.pi, "e": self.mp.e, "tau": 2 * self.mp.pi}[nombre])
        return self.real({"pi": math.pi, "e": math.e, "tau": math.tau}[nombre])

    # -- operaciones
    def _mezcla(self, a, b, op_exacta, op_mp):
        if isinstance(a, Aprox) or isinstance(b, Aprox) or isinstance(a, complex) or isinstance(b, complex):
            if self.modo == "float":
                return op_mp(self.flotante(a), self.flotante(b))
            return self.real(op_mp(self.a_mp(a), self.a_mp(b)))
        if self.modo == "float" and (isinstance(a, float) or isinstance(b, float)):
            return op_mp(float(a), float(b))
        return _check(_num(op_exacta(a, b)))

    def suma(self, a, b):
        return self._mezcla(a, b, operator.add, operator.add)

    def resta(self, a, b):
        return self._mezcla(a, b, operator.sub, operator.sub)

    def mul(self, a, b):
        return self._mezcla(a, b, operator.mul, operator.mul)

    def div(self, a, b):
        if not isinstance(b, Aprox) and b == 0:
            raise Fallo("División por cero")
        return self._mezcla(a, b, lambda x, y: Fraction(x) / Fraction(y), operator.truediv)

    def pot(self, a, b):
        b = _num(b)
        if isinstance(b, int) and not isinstance(a, Aprox) and self.modo != "float":
            if abs(b) > 100_000 or (isinstance(a, (int, Fraction)) and abs(b) * max(1, Fraction(a).numerator.bit_length(),
                                                                                    Fraction(a).denominator.bit_length()) > MAX_BITS):
                raise Fallo("Potencia demasiado grande para calcularla exacta")
            if a == 0 and b < 0:
                raise Fallo("División por cero")
            return _check(_num(Fraction(a) ** b))
        if isinstance(b, Fraction) and not isinstance(a, Aprox) and self.modo != "float" and a > 0 \
                and abs(b.numerator) <= 64 and b.denominator <= 64:
            return self.raiz(_check(_num(Fraction(a) ** b.numerator)), b.denominator)   # 8^(2/3) = 4 exacto
        if self.modo == "float":
            return self.flotante(a) ** self.flotante(b)
        if self.mp:
            return self.real(self.a_mp(a) ** self.a_mp(b))
        return self.real(float(a) ** float(b))

    # -- árbol
    def ev(self, nodo):
        if isinstance(nodo, ast.Expression):
            return self.ev(nodo.body)
        if isinstance(nodo, ast.Constant):
            v = nodo.value
            if isinstance(v, bool):
                return v
            if isinstance(v, int):
                return v
            if isinstance(v, float):
                return v if self.modo == "float" else Fraction(nodo.texto) if hasattr(nodo, "texto") else Fraction(repr(v))
            if isinstance(v, complex):
                raise Fallo("Números complejos: usa la herramienta algebra")
            raise Fallo(f"Valor no admitido: {v!r}")
        if isinstance(nodo, ast.Name):
            n = nodo.id
            if n in self.vars:
                return self.vars[n]
            if n in ("pi", "e", "tau"):
                return self.const(n)
            if n in ("True", "False"):
                return n == "True"
            raise Fallo(f"Nombre desconocido: {n}")
        if isinstance(nodo, ast.UnaryOp):
            v = self.ev(nodo.operand)
            if isinstance(nodo.op, ast.USub):
                return self.mul(-1, v)
            if isinstance(nodo.op, ast.UAdd):
                return v
            if isinstance(nodo.op, ast.Not):
                return not v
            if isinstance(nodo.op, ast.Invert):
                return ~self.entero(v)
        if isinstance(nodo, ast.BinOp):
            a, b = self.ev(nodo.left), self.ev(nodo.right)
            op = type(nodo.op)
            tabla = {ast.Add: self.suma, ast.Sub: self.resta, ast.Mult: self.mul, ast.Div: self.div,
                     ast.Pow: self.pot}
            if op in tabla:
                return tabla[op](a, b)
            if op in (ast.FloorDiv, ast.Mod):
                if isinstance(a, Aprox) or isinstance(b, Aprox):
                    a, b = Fraction(str(self.flotante(a))), Fraction(str(self.flotante(b)))
                if b == 0:
                    raise Fallo("División por cero")
                return _num(Fraction(a) // Fraction(b) if op is ast.FloorDiv else Fraction(a) % Fraction(b))
            bits = {ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor,
                    ast.LShift: operator.lshift, ast.RShift: operator.rshift}
            if op in bits:
                x, y = self.entero(a), self.entero(b)
                if op is ast.LShift and y > MAX_BITS:
                    raise Fallo("Desplazamiento demasiado grande")
                return bits[op](x, y)
        if isinstance(nodo, ast.Compare):
            izq = self.ev(nodo.left)
            for op, der_n in zip(nodo.ops, nodo.comparators):
                der = self.ev(der_n)
                ops = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
                       ast.Gt: operator.gt, ast.GtE: operator.ge}
                if type(op) not in ops:
                    raise Fallo("Comparación no admitida")
                if isinstance(izq, Aprox) or isinstance(der, Aprox):
                    # Con irracionales la igualdad es a la precisión pedida (no la de un double)
                    dif = self.resta(izq, der)
                    tol = Fraction(1, 10 ** (self.dig - 3)) * max(1, abs(Fraction(self.flotante(der))))
                    dv = abs(Fraction(str(dif.v)) if isinstance(dif, Aprox) and self.mp else Fraction(self.flotante(dif)))
                    if type(op) in (ast.Eq, ast.NotEq):
                        ok = (dv <= tol) == (type(op) is ast.Eq)
                    else:
                        ok = ops[type(op)](self.flotante(izq), self.flotante(der))
                elif isinstance(izq, bool) or isinstance(der, bool):
                    ok = ops[type(op)](izq, der)
                else:
                    ok = ops[type(op)](Fraction(izq), Fraction(der)) if not isinstance(izq, float) else ops[type(op)](izq, der)
                if not ok:
                    return False
                izq = der
            return True
        if isinstance(nodo, ast.BoolOp):
            vals = [self.ev(v) for v in nodo.values]
            return all(vals) if isinstance(nodo.op, ast.And) else any(vals)
        if isinstance(nodo, ast.Call):
            if not isinstance(nodo.func, ast.Name) or nodo.keywords:
                raise Fallo("Solo se admiten llamadas a funciones matemáticas por su nombre")
            fns = self.funciones()
            if nodo.func.id not in fns:
                raise Fallo(f"Función desconocida: {nodo.func.id}. Hay: {', '.join(sorted(fns))}")
            return fns[nodo.func.id](*[self.ev(a) for a in nodo.args])
        raise Fallo(f"Expresión no admitida: {type(nodo).__name__}")


def _iroot(n: int, k: int):
    """Raíz k-ésima entera exacta o None."""
    if n < 0:
        return None
    if n in (0, 1):
        return n
    r = int(round(n ** (1.0 / k))) if n.bit_length() < 1000 else _iroot_newton(n, k)
    for c in (r - 1, r, r + 1):
        if c >= 0 and c ** k == n:
            return c
    return None


def _iroot_newton(n: int, k: int) -> int:
    x = 1 << ((n.bit_length() + k - 1) // k)
    while True:
        y = ((k - 1) * x + n // x ** (k - 1)) // k
        if y >= x:
            return x
        x = y


_DEC = re.compile(r"(?<![\w.])(\d+\.\d*|\.\d+|\d+)(?:[eE]([+-]?\d+))?(?![\w.])")


def preparar(expr: str) -> str:
    """Notación habitual: «^» como potencia, «×»/«·» y «÷», coma decimal «3,5» si no hay comas de argumentos."""
    t = expr.strip()
    t = t.replace("^", "**").replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-")
    # Sin letras no hay funciones con varios argumentos: «3,5» es la coma decimal
    if "," in t and re.fullmatch(r"[\d\s+\-*/().,%]*", t):
        t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\w(.])", r"(\1/100)", t)     # 15% → 15/100; «7 % 3» sigue siendo módulo
    return t


def parsear(expr: str) -> ast.Expression:
    if len(expr) > 5000:
        raise Fallo("Expresión demasiado larga")
    try:
        arbol = ast.parse(preparar(expr), mode="eval")
    except SyntaxError as e:
        raise Fallo(f"No entiendo la expresión: {e.msg}") from e
    # Decimales exactos: 0.1 es 1/10, no el doble más cercano
    fuente = preparar(expr)
    for n in ast.walk(arbol):
        if isinstance(n, ast.Constant) and isinstance(n.value, float) and hasattr(n, "col_offset"):
            seg = ast.get_source_segment(fuente, n)
            if seg and re.fullmatch(r"[\d.]+(?:[eE][+-]?\d+)?", seg):
                n.texto = seg
    return arbol


def fmt(x, digitos: int = 30) -> dict:
    if isinstance(x, bool):
        return {"valor": "verdadero" if x else "falso", "exacto": True}
    x = _num(x)
    if isinstance(x, int):
        return {"valor": str(x), "exacto": True}
    if isinstance(x, Fraction):
        getcontext().prec = digitos
        dec = Decimal(x.numerator) / Decimal(x.denominator)
        return {"valor": f"{x.numerator}/{x.denominator}", "decimal": _dec(dec, digitos), "exacto": True}
    if isinstance(x, Aprox):
        mp = _opcional("mpmath")
        txt = mp.nstr(x.v, digitos) if mp and not isinstance(x.v, float) else repr(float(x.v))
        return {"valor": txt, "exacto": False,
                "precision": f"{digitos} cifras significativas" if mp and not isinstance(x.v, float) else "doble (≈15 cifras)"}
    if isinstance(x, float):
        return {"valor": repr(x), "exacto": False, "precision": "doble (≈15 cifras)"}
    return {"valor": str(x), "exacto": True}


def _dec(d: Decimal, digitos: int) -> str:
    s = format(d.normalize(), "f") if abs(d.adjusted()) < digitos else format(d, "E")
    return s


# ---------------------------------------------------------------- herramientas
def calcular(a: dict) -> str:
    expr = str(a.get("expresion") or "")
    dig = int(a.get("digitos") or 30)
    ev = Evaluador(dig)
    r = ev.ev(parsear(expr))
    if isinstance(r, str):
        return f"{expr} = {r}"
    if isinstance(r, bool):
        return f"{expr} → {'verdadero' if r else 'falso'}" + \
            (f"\nComparación con irracionales: igualdad comprobada a {ev.dig - 3} cifras." if ev.aprox else
             "\nComprobación exacta.")
    f = fmt(r, ev.dig)
    lineas = [f"{expr} = {f['valor']}"]
    if "decimal" in f:
        den = Fraction(_num(r)).denominator
        while den % 2 == 0:
            den //= 2
        while den % 5 == 0:
            den //= 5
        lineas.append(("= " if den == 1 else "≈ ") + f["decimal"] +
                      ("" if den == 1 else f" (decimal periódico, mostrado con {ev.dig} cifras)"))
    lineas.append("Resultado exacto." if f["exacto"] else f"Resultado aproximado: {f['precision']}.")
    return "\n".join(lineas)


def unidades(a: dict) -> str:
    pint = _opcional("pint")
    if not pint:
        raise falta("python3-pint")
    ureg = pint.UnitRegistry()
    expr = preparar(str(a.get("expresion") or ""))
    destino = str(a.get("a") or "").strip()
    try:
        q = ureg.parse_expression(expr)
        if destino:
            q = q.to(destino)
        elif hasattr(q, "to_reduced_units"):
            q = q.to_reduced_units()
    except pint.errors.DimensionalityError as e:
        return (f"Las dimensiones no cuadran: {e.dim1 or e.units1} no se puede convertir en {e.dim2 or e.units2}. "
                "Revisa la fórmula: probablemente falta o sobra una magnitud.")
    except (pint.errors.UndefinedUnitError, pint.errors.DefinitionSyntaxError) as e:
        return f"Unidad desconocida: {e}"
    except Exception as e:  # noqa: BLE001 (errores de sintaxis de pint)
        return f"No entiendo la expresión: {e}"
    if not hasattr(q, "magnitude"):
        return f"{expr} = {q} (sin unidades)"
    mag = q.magnitude
    txt = f"{mag:.10g}" if isinstance(mag, float) else str(mag)
    dim = str(q.dimensionality)
    return f"{expr} = {txt} {q.units:~P}\nDimensión: {dim if dim != 'dimensionless' else 'adimensional'}\n" \
           f"En unidades SI base: {q.to_base_units():.10g~P}"


_PELIGRO = re.compile(r"__|\bimport\b|\blambda\b|\bexec\b|\beval\b|\bopen\b|\bglobals\b|\blocals\b|\bgetattr\b|"
                      r"\bcompile\b|\binput\b|[\[\]{}]|\.[A-Za-z_]|['\"`\\;@!$]")


def _sympy_expr(texto: str, sympy, locales: dict | None = None):
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                            parse_expr, standard_transformations)
    t = preparar(texto)
    if _PELIGRO.search(t):
        raise Fallo("La expresión tiene caracteres o palabras no permitidos (solo matemática: números, "
                    "variables, + - * / ^ ( ) y funciones)")
    if len(t) > 3000:
        raise Fallo("Expresión demasiado larga")
    trans = standard_transformations + (implicit_multiplication_application, convert_xor)
    loc = {"e": sympy.E, "pi": sympy.pi, "i": sympy.I, "I": sympy.I, "ln": sympy.log, "sen": sympy.sin,
           "raiz": sympy.sqrt}
    loc.update(locales or {})
    return parse_expr(t, local_dict=loc, transformations=trans, evaluate=True)


def _sympy_ecuacion(texto: str, sympy):
    if "=" in texto and "==" not in texto:
        izq, der = texto.split("=", 1)
        return sympy.Eq(_sympy_expr(izq, sympy), _sympy_expr(der, sympy))
    return _sympy_expr(texto.replace("==", "="), sympy) if "==" not in texto else \
        sympy.Eq(*[_sympy_expr(p, sympy) for p in texto.split("==", 1)])


def _matriz(texto: str, sympy):
    filas = [f for f in re.split(r";|\n", texto.strip().strip("[]")) if f.strip()]
    datos = []
    for f in filas:
        celdas = [c for c in re.split(r"[,\s]+", f.strip().strip("[]")) if c]
        datos.append([_sympy_expr(c, sympy) for c in celdas])
    if not datos or len({len(f) for f in datos}) != 1:
        raise Fallo("Matriz mal escrita: filas separadas por «;» y elementos por comas o espacios, p. ej. 1 2; 3 4")
    return sympy.Matrix(datos)


def algebra(a: dict) -> str:
    sympy = _opcional("sympy")
    if not sympy:
        raise falta("python3-sympy")
    op = str(a.get("operacion") or "simplificar")
    expr = str(a.get("expresion") or "")
    var = sympy.Symbol(str(a.get("variable") or "x")) if re.fullmatch(r"[A-Za-z]\w{0,15}", str(a.get("variable") or "x")) \
        else sympy.Symbol("x")
    if op == "matriz":
        m = _matriz(expr, sympy)
        q = str(a.get("calculo") or "todo")
        out = [f"Matriz {m.shape[0]}×{m.shape[1]}"]
        if m.shape[0] == m.shape[1]:
            det = m.det()
            out.append(f"determinante = {det}")
            if q in ("todo", "inversa") and det != 0:
                out.append(f"inversa = {m.inv().tolist()}")
            if q in ("todo", "autovalores"):
                out.append(f"autovalores (valor: multiplicidad) = {m.eigenvals()}")
        out.append(f"rango = {m.rank()}")
        if q in ("todo", "rref"):
            out.append(f"forma escalonada reducida = {m.rref()[0].tolist()}")
        return "\n".join(out)
    if op == "sistema":
        ecs = [_sympy_ecuacion(e, sympy) for e in re.split(r";|\n", expr) if e.strip()]
        incog = sorted(set().union(*(e.free_symbols for e in ecs)), key=str)
        sol = sympy.solve(ecs, incog, dict=True)
        return f"Incógnitas: {', '.join(map(str, incog))}\nSoluciones: {sol if sol else 'ninguna'}"
    if op == "resolver":
        ec = _sympy_ecuacion(expr, sympy)
        libres = sorted(getattr(ec, "free_symbols", set()), key=str)
        v = var if var in libres or not libres else libres[0]
        sol = sympy.solve(ec, v)
        aprox = [sympy.N(s, 15) for s in sol]
        return f"{v} = {sol}" + (f"\n≈ {aprox}" if sol and any(not s.is_Integer for s in sol) else "") + \
            ("" if sol else "\nSin solución simbólica: prueba la herramienta numerico (raíz).")
    if op == "edo":
        f = sympy.Function(str(a.get("funcion") or "y"))
        t = var
        texto = preparar(expr)
        nombre = str(a.get("funcion") or "y")
        if not re.fullmatch(r"[A-Za-z]\w{0,15}", nombre):
            raise Fallo("Nombre de función inválido")
        # y'' → Derivative(y(t), t, 2); y' → Derivative(y(t), t); y → y(t)
        for k in (3, 2, 1):
            texto = re.sub(rf"\b{nombre}{'′' * k}|\b{nombre}{chr(39) * k}", f"__D{k}__", texto)
        if _PELIGRO.search(texto.replace("__D1__", "").replace("__D2__", "").replace("__D3__", "")):
            raise Fallo("La ecuación tiene caracteres no permitidos")
        izq, _, der = texto.partition("=")
        locales = {nombre: f(t)}

        def conv(s):
            s = s.replace("__D3__", "D3").replace("__D2__", "D2").replace("__D1__", "D1")
            return _sympy_expr(s or "0", sympy, {**locales, "D1": f(t).diff(t), "D2": f(t).diff(t, 2),
                                                 "D3": f(t).diff(t, 3)})
        ec = sympy.Eq(conv(izq), conv(der))
        ics = {}
        for ci in re.split(r";|\n", str(a.get("condiciones") or "")):
            m = re.fullmatch(rf"\s*{nombre}('*)\(([^)]+)\)\s*=\s*(.+)", ci)
            if m:
                orden, x0, val = len(m.group(1)), _sympy_expr(m.group(2), sympy), _sympy_expr(m.group(3), sympy)
                ics[f(t).diff(t, orden).subs(t, x0) if orden else f(x0)] = val
        sol = sympy.dsolve(ec, f(t), ics=ics or None)
        return f"Ecuación: {ec}\nSolución: {sol}"
    e = _sympy_expr(expr, sympy)
    if op == "simplificar":
        return f"{sympy.simplify(e)}"
    if op == "factorizar":
        return f"{sympy.factor(e)}"
    if op == "expandir":
        return f"{sympy.expand(e)}"
    if op == "derivar":
        n = int(a.get("orden") or 1)
        return f"d^{n}/d{var}^{n} [{e}] = {sympy.simplify(sympy.diff(e, var, n))}"
    if op == "integrar":
        lim = a.get("limites")
        if lim:
            lo, hi = [_sympy_expr(str(x), sympy) for x in (lim if isinstance(lim, list) else str(lim).split(","))][:2]
            r = sympy.integrate(e, (var, lo, hi))
            return f"∫ {e} d{var} de {lo} a {hi} = {r}" + (f" ≈ {sympy.N(r, 20)}" if not r.is_Integer else "")
        return f"∫ {e} d{var} = {sympy.integrate(e, var)} + C"
    if op == "limite":
        p = _sympy_expr(str(a.get("punto") or "0").replace("inf", "oo"), sympy, {"oo": sympy.oo})
        return f"lím {var}→{p} {e} = {sympy.limit(e, var, p)}"
    if op == "serie":
        p = _sympy_expr(str(a.get("punto") or "0"), sympy)
        return f"{sympy.series(e, var, p, int(a.get('orden') or 6))}"
    if op == "evaluar":
        vals = {sympy.Symbol(k): _sympy_expr(str(v), sympy) for k, v in (a.get("valores") or {}).items()
                if re.fullmatch(r"[A-Za-z]\w{0,15}", k)}
        r = e.subs(vals)
        return f"{r}" + (f" ≈ {sympy.N(r, int(a.get('digitos') or 20))}" if r.free_symbols == set() else "")
    raise Fallo("operación desconocida")


# ---------------------------------------------------------------- numérico
def _funcion(expr: str, variables: list[str]):
    arbol = parsear(expr)

    def f(*vals):
        ev = Evaluador(variables=dict(zip(variables, vals)), modo="float")
        return float(ev.flotante(ev.ev(arbol)))
    return f


def numerico(a: dict) -> str:
    op = str(a.get("operacion") or "")
    scipy = _opcional("scipy")
    if op == "raiz":
        f = _funcion(str(a["expresion"]), [str(a.get("variable") or "x")])
        lo, hi = [float(v) for v in a.get("intervalo") or [0, 1]][:2]
        flo, fhi = f(lo), f(hi)
        if flo * fhi > 0:
            return (f"f({lo}) = {flo:.6g} y f({hi}) = {fhi:.6g} tienen el mismo signo: elige un intervalo donde la "
                    "función cambie de signo.")
        for _ in range(200):                         # bisección: garantizada, sin depender de scipy
            mid = (lo + hi) / 2
            fm = f(mid)
            if fm == 0 or hi - lo < 1e-15 * max(1.0, abs(mid)):
                break
            if flo * fm < 0:
                hi = mid
            else:
                lo, flo = mid, fm
        return f"raíz ≈ {mid:.15g} (f = {f(mid):.3g}; garantizada por bisección en el intervalo dado)"
    if op == "integral":
        f = _funcion(str(a["expresion"]), [str(a.get("variable") or "x")])
        lo, hi = [float(v) for v in a.get("intervalo") or [0, 1]][:2]
        if scipy:
            from scipy import integrate
            v, err = integrate.quad(f, lo, hi, limit=200)
            return f"∫ = {v:.15g} (error estimado {err:.2g})"
        n = 2000                                     # Simpson
        h = (hi - lo) / n
        s = f(lo) + f(hi) + sum((4 if i % 2 else 2) * f(lo + i * h) for i in range(1, n))
        return f"∫ ≈ {s * h / 3:.12g} (Simpson con {n} intervalos)"
    if op == "minimizar":
        if not scipy:
            raise falta("python3-scipy")
        from scipy import optimize
        vs = [str(v) for v in a.get("variables") or ["x"]]
        f = _funcion(str(a["expresion"]), vs)
        x0 = [float(v) for v in a.get("inicio") or [0] * len(vs)]
        r = optimize.minimize(lambda x: f(*x), x0, method="Nelder-Mead" if len(vs) > 1 else "BFGS",
                              options={"maxiter": 20000, "xatol": 1e-12, "fatol": 1e-14} if len(vs) > 1 else {})
        return f"mínimo local en {dict(zip(vs, (float(v) for v in r.x)))}, f = {float(r.fun):.12g} " \
               f"({'convergió' if r.success else 'NO convergió: ' + str(r.message)})"
    if op == "edo":
        if not scipy:
            raise falta("python3-scipy")
        from scipy.integrate import solve_ivp
        ecs = [e for e in re.split(r";|\n", str(a["expresion"])) if e.strip()]
        nombres = [str(v) for v in a.get("variables") or [f"y{i}" for i in range(len(ecs))]]
        t_var = str(a.get("variable") or "t")
        fs = [_funcion(e.split("=", 1)[-1], [t_var, *nombres]) for e in ecs]
        y0 = [float(v) for v in a.get("inicio") or [0] * len(ecs)]
        t0, t1 = [float(v) for v in a.get("intervalo") or [0, 1]][:2]
        r = solve_ivp(lambda t, y: [f(t, *y) for f in fs], (t0, t1), y0, rtol=1e-10, atol=1e-12, dense_output=True)
        pts = int(a.get("puntos") or 6)
        filas = [f"{t_var}={t:.6g}: " + ", ".join(f"{n}={v:.10g}" for n, v in zip(nombres, r.sol(t)))
                 for t in [t0 + (t1 - t0) * i / (pts - 1) for i in range(pts)]]
        return ("RK45 con tolerancia 1e-10" + ("" if r.success else f" (aviso: {r.message})") + "\n" + "\n".join(filas))
    if op == "ajuste":
        if not scipy:
            raise falta("python3-scipy")
        from scipy.optimize import curve_fit
        params = [str(p) for p in a.get("parametros") or []]
        x = [float(v) for v in a["x"]]
        y = [float(v) for v in a["y"]]
        f = _funcion(str(a["expresion"]), [str(a.get("variable") or "x"), *params])
        popt, pcov = curve_fit(lambda xx, *p: [f(xi, *p) for xi in xx], x, y, p0=[1.0] * len(params), maxfev=20000)
        res = [yi - f(xi, *popt) for xi, yi in zip(x, y)]
        ss_res = sum(r * r for r in res)
        my = sum(y) / len(y)
        ss_tot = sum((yi - my) ** 2 for yi in y) or 1e-300
        err = [math.sqrt(pcov[i][i]) if pcov[i][i] >= 0 else float("nan") for i in range(len(params))]
        return "\n".join(f"{p} = {v:.10g} ± {e:.2g}" for p, v, e in zip(params, popt, err)) + \
            f"\nR² = {1 - ss_res / ss_tot:.8f}"
    raise Fallo("operación desconocida: usa raiz, integral, minimizar, edo o ajuste")


# ---------------------------------------------------------------- estadística
def _datos(a: dict, clave: str = "datos") -> list[float]:
    d = a.get(clave)
    if isinstance(d, str):
        d = [x for x in re.split(r"[,;\s]+", d.strip()) if x]
    if not d:
        raise Fallo(f"Faltan los datos ({clave})")
    try:
        return [float(str(x).replace(",", ".")) if not isinstance(x, (int, float)) else float(x) for x in d]
    except ValueError as e:
        raise Fallo(f"Dato no numérico: {e}") from e


def _csv(ruta: str, columna: str) -> list[float]:
    import csv
    from pathlib import Path
    p = Path(ruta)
    if not p.is_file():
        raise Fallo(f"No existe el archivo {ruta}")
    with open(p, newline="", encoding="utf-8", errors="replace") as fh:
        muestra = fh.read(4096)
        fh.seek(0)
        dialecto = csv.Sniffer().sniff(muestra, delimiters=",;\t") if muestra else csv.excel
        filas = csv.DictReader(fh, dialect=dialecto)
        out = []
        for i, fila in enumerate(filas):
            if i > 2_000_000:
                break
            v = (fila.get(columna) or "").strip().replace(",", ".")
            if v:
                try:
                    out.append(float(v))
                except ValueError:
                    continue
        if not out:
            raise Fallo(f"La columna «{columna}» no tiene números (columnas: {', '.join(filas.fieldnames or [])})")
        return out


def estadistica(a: dict) -> str:
    import statistics as st
    op = str(a.get("operacion") or "describir")
    if a.get("archivo"):
        datos = _csv(str(a["archivo"]), str(a.get("columna") or ""))
    elif op not in ("regresion", "correlacion"):
        datos = _datos(a)
    scipy = _opcional("scipy")
    if op == "describir":
        n = len(datos)
        q = st.quantiles(datos, n=4, method="inclusive") if n >= 2 else [datos[0]] * 3
        out = [f"n = {n}", f"media = {st.fmean(datos):.10g}", f"mediana = {st.median(datos):.10g}",
               f"mínimo = {min(datos):.10g}", f"máximo = {max(datos):.10g}",
               f"cuartiles Q1/Q2/Q3 = {q[0]:.10g} / {q[1]:.10g} / {q[2]:.10g} (IQR {q[2] - q[0]:.10g})"]
        if n >= 2:
            s = st.stdev(datos)
            out += [f"desviación típica (muestral) = {s:.10g}", f"varianza (muestral) = {st.variance(datos):.10g}",
                    f"error típico de la media = {s / math.sqrt(n):.6g}"]
            if scipy:
                from scipy import stats
                t = stats.t.ppf(0.975, n - 1)
                m = st.fmean(datos)
                out.append(f"IC 95 % de la media = [{m - t * s / math.sqrt(n):.8g}, {m + t * s / math.sqrt(n):.8g}]")
        return "\n".join(out)
    if op in ("regresion", "correlacion"):
        x, y = _datos(a, "x"), _datos(a, "y")
        if len(x) != len(y) or len(x) < 3:
            raise Fallo("x e y deben tener la misma cantidad de datos (al menos 3)")
        r = st.correlation(x, y)
        if op == "correlacion":
            out = f"r de Pearson = {r:.10g} (r² = {r * r:.6g})"
            if scipy:
                from scipy import stats
                out += f"\np = {stats.pearsonr(x, y).pvalue:.4g}; rho de Spearman = {stats.spearmanr(x, y).statistic:.6g}"
            return out
        lr = st.linear_regression(x, y)
        out = f"y = {lr.slope:.10g}·x + {lr.intercept:.10g}\nR² = {r * r:.8f}"
        if scipy:
            from scipy import stats
            res = stats.linregress(x, y)
            out += f"\nerror de la pendiente = {res.stderr:.4g}, p = {res.pvalue:.4g}"
        return out
    if op == "prueba_t":
        if not scipy:
            raise falta("python3-scipy")
        from scipy import stats
        b = _datos(a, "datos_b")
        r = stats.ttest_ind(datos, b, equal_var=False)
        return f"t de Welch = {r.statistic:.6g}, p = {r.pvalue:.4g} " \
               f"({'diferencia significativa' if r.pvalue < 0.05 else 'sin diferencia significativa'} al 5 %)"
    raise Fallo("operación desconocida: describir, regresion, correlacion o prueba_t")


# ---------------------------------------------------------------- lógica (z3)
def logica(a: dict) -> str:
    z3 = _opcional("z3")
    if not z3:
        raise falta("python3-z3")
    smt = str(a.get("smtlib") or "")
    if len(smt) > 20000:
        raise Fallo("Problema demasiado largo")
    s = z3.Solver()
    s.set("timeout", 20000)
    try:
        s.add(z3.parse_smt2_string(smt))
    except z3.Z3Exception as e:
        raise Fallo(f"SMT-LIB no válido: {e}") from e
    r = s.check()
    if r == z3.sat:
        return "sat (satisfacible). Modelo:\n" + "\n".join(f"{d.name()} = {s.model()[d]}" for d in s.model().decls())
    if r == z3.unsat:
        return "unsat (insatisfacible): no existe ninguna asignación. Si afirmaste la NEGACIÓN de una propiedad, " \
               "la propiedad queda demostrada."
    return f"desconocido ({s.reason_unknown()})"


# ---------------------------------------------------------------- bits
def bits(a: dict) -> str:
    import struct
    op = str(a.get("operacion") or "convertir")
    v = str(a.get("valor") or "").strip().replace("_", "")
    ancho = int(a.get("ancho") or 0)
    if op == "ieee754":
        tipo = str(a.get("tipo") or "float64")
        fmt_c, nb = ("f", 4) if tipo == "float32" else ("d", 8)
        if re.fullmatch(r"0x[0-9a-fA-F]+", v):
            raw = int(v, 16).to_bytes(nb, "big")
            x = struct.unpack(">" + fmt_c, raw)[0]
        else:
            x = float(v.replace(",", "."))
            raw = struct.pack(">" + fmt_c, x)
        n = int.from_bytes(raw, "big")
        eb, mb = (8, 23) if nb == 4 else (11, 52)
        signo, exp, mant = n >> (eb + mb), (n >> mb) & ((1 << eb) - 1), n & ((1 << mb) - 1)
        return (f"{tipo}: {x!r}\nhex = 0x{n:0{nb * 2}X}\nbits = {signo} {exp:0{eb}b} {mant:0{mb}b}\n"
                f"signo={signo}, exponente={exp} (sin sesgo {exp - (2 ** (eb - 1) - 1)}), mantisa=0x{mant:X}\n"
                f"valor exacto almacenado = {Fraction(struct.unpack('>' + fmt_c, raw)[0]) if math.isfinite(x) else x}")
    try:
        n = int(v, 0)
    except ValueError:
        try:
            n = Evaluador().entero(Evaluador().ev(parsear(v)))     # p. ej. «0xFF & 0b1010» o «2**31-1»
        except Fallo as e:
            raise Fallo(f"Número no válido: {v}") from e
    out = []
    if ancho:
        if not 1 <= ancho <= 4096:
            raise Fallo("ancho entre 1 y 4096 bits")
        u = n & ((1 << ancho) - 1)
        s = u - (1 << ancho) if u >> (ancho - 1) else u
        out.append(f"en {ancho} bits: sin signo = {u}, con signo (complemento a dos) = {s}")
        if not -(1 << (ancho - 1)) <= n < (1 << ancho):
            out.append(f"aviso: {n} no cabe en {ancho} bits (se truncó)")
        n_vis = u
        out.append(f"bin = 0b{u:0{ancho}b}\nhex = 0x{u:0{(ancho + 3) // 4}X}")
        if ancho % 8 == 0:
            out.append(f"bytes big-endian = {u.to_bytes(ancho // 8, 'big').hex(' ')}; "
                       f"little-endian = {u.to_bytes(ancho // 8, 'little').hex(' ')}")
    else:
        n_vis = n
        out.append(f"dec = {n}\nhex = {hex(n)}\nbin = {bin(n)}\noct = {oct(n)}")
    out.append(f"bits a 1 = {bin(abs(n_vis)).count('1')}, longitud = {abs(n_vis).bit_length()} bits")
    return "\n".join(out)


# ---------------------------------------------------------------- fechas
def _fecha(s: str):
    import datetime as dt
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            continue
    if s.lower() in ("hoy", "ahora"):
        return dt.datetime.now().replace(microsecond=0)
    raise Fallo(f"Fecha no reconocida: «{s}» (usa AAAA-MM-DD [HH:MM] o DD/MM/AAAA)")


_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def fechas(a: dict) -> str:
    import datetime as dt
    op = str(a.get("operacion") or "diferencia")
    if op == "diferencia":
        d1, d2 = _fecha(str(a["desde"])), _fecha(str(a["hasta"]))
        delta = d2 - d1
        lab = 0
        paso = 1 if d2 >= d1 else -1
        dia = d1.date()
        while dia != d2.date() and abs(lab) < 100000:
            if dia.weekday() < 5:
                lab += paso
            dia += dt.timedelta(days=paso)
        meses = (d2.year - d1.year) * 12 + d2.month - d1.month - (1 if d2.day < d1.day else 0)
        return (f"{delta.days} días ({delta.days // 7} semanas y {delta.days % 7} días), "
                f"{delta.total_seconds() / 3600:.6g} horas en total\n"
                f"meses completos = {meses}; días laborables (lun-vie, sin festivos) = {lab}")
    if op == "sumar":
        d = _fecha(str(a["desde"]))
        txt = str(a.get("duracion") or "")
        partes = dict.fromkeys(("dias", "horas", "minutos", "semanas", "meses", "anos"), 0)
        for n, u in re.findall(r"(-?\d+)\s*(d[ií]as?|horas?|minutos?|semanas?|mes(?:es)?|a[nñ]os?)", txt, re.I):
            u = u.lower()
            k = ("dias" if u.startswith("d") else "horas" if u.startswith("h") else "minutos" if u.startswith("mi")
                 else "semanas" if u.startswith("s") else "meses" if u.startswith("me") else "anos")
            partes[k] += int(n)
        m = d.month - 1 + partes["meses"] + 12 * partes["anos"]
        y, m = d.year + m // 12, m % 12 + 1
        import calendar
        d = d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))
        d += dt.timedelta(days=partes["dias"] + 7 * partes["semanas"], hours=partes["horas"], minutes=partes["minutos"])
        return f"{d:%Y-%m-%d %H:%M} ({_DIAS[d.weekday()]})"
    if op == "dia":
        d = _fecha(str(a["desde"]))
        iso = d.isocalendar()
        return f"{d:%Y-%m-%d}: {_DIAS[d.weekday()]}, semana ISO {iso.week} de {iso.year}, día {d.timetuple().tm_yday} del año"
    if op == "zona":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            origen, destino = ZoneInfo(str(a["zona_origen"])), ZoneInfo(str(a["zona_destino"]))
        except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
            raise Fallo(f"Zona horaria desconocida (usa nombres IANA como America/Montevideo): {e}") from e
        d = _fecha(str(a["desde"])).replace(tzinfo=origen)
        r = d.astimezone(destino)
        return f"{d:%Y-%m-%d %H:%M %Z (UTC%z)} = {r:%Y-%m-%d %H:%M %Z (UTC%z)}"
    raise Fallo("operación desconocida: diferencia, sumar, dia o zona")


# ---------------------------------------------------------------- huellas
def huella(a: dict) -> str:
    import hashlib
    import zlib
    from pathlib import Path
    alg = str(a.get("algoritmo") or "sha256").lower()
    if a.get("archivo"):
        p = Path(str(a["archivo"]))
        if not p.is_file():
            raise Fallo(f"No existe el archivo {a['archivo']}")
        datos = p.read_bytes()[: 1 << 30]
    else:
        datos = str(a.get("texto") or "").encode()
    if alg == "crc32":
        return f"crc32 = {zlib.crc32(datos):08x}"
    if alg == "adler32":
        return f"adler32 = {zlib.adler32(datos):08x}"
    if alg not in hashlib.algorithms_guaranteed:
        raise Fallo(f"Algoritmo desconocido. Hay: crc32, adler32, {', '.join(sorted(hashlib.algorithms_guaranteed))}")
    h = hashlib.new(alg, datos)
    return f"{alg} = {h.hexdigest() if not alg.startswith('shake') else h.hexdigest(32)}  ({len(datos)} bytes)"


# ---------------------------------------------------------------- constantes
def constantes(a: dict) -> str:
    scipy = _opcional("scipy")
    if not scipy:
        raise falta("python3-scipy")
    from scipy import constants as c
    q = str(a.get("nombre") or "").lower().strip()
    if not q:
        raise Fallo("Indica qué constante buscas (p. ej. «planck», «electron mass», «boltzmann»)")
    alias = {"planck": "planck constant", "boltzmann": "boltzmann constant", "avogadro": "avogadro constant",
             "luz": "speed of light in vacuum", "velocidad de la luz": "speed of light in vacuum",
             "carga del electron": "elementary charge", "carga elemental": "elementary charge",
             "masa del electron": "electron mass", "masa del proton": "proton mass", "gravitacion": "newtonian constant of gravitation",
             "g": "standard acceleration of gravity", "gravedad": "standard acceleration of gravity",
             "gases": "molar gas constant", "r": "molar gas constant", "permitividad": "vacuum electric permittivity",
             "permeabilidad": "vacuum mag. permeability", "stefan": "stefan-boltzmann constant",
             "faraday": "faraday constant", "rydberg": "rydberg constant", "bohr": "bohr radius"}
    q = alias.get(q, q)
    claves = [k for k in c.physical_constants if q in k.lower()]
    if not claves:
        return "No encontré esa constante. Prueba con su nombre en inglés de CODATA (p. ej. «neutron mass»)."
    out = []
    for k in sorted(claves, key=len)[:8]:
        v, u, inc = c.physical_constants[k]
        out.append(f"{k} = {v!r} {u}" + (" (exacta por definición)" if inc == 0 else f" ± {inc:.2g}"))
    return "Fuente: CODATA (scipy.constants)\n" + "\n".join(out)


HERRAMIENTAS = {"calcular": calcular, "unidades": unidades, "algebra": algebra, "numerico": numerico,
                "estadistica": estadistica, "logica": logica, "bits": bits, "fechas": fechas, "huella": huella,
                "constantes": constantes}


def main() -> int:
    try:
        pedido = json.loads(sys.stdin.read(1 << 20))
        fn = HERRAMIENTAS[pedido["herramienta"]]
        res = {"ok": True, "resultado": fn(pedido.get("args") or {})}
    except Fallo as e:
        res = {"ok": False, "resultado": str(e)}
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError, ArithmeticError, RecursionError) as e:
        res = {"ok": False, "resultado": f"Error: {type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 (sympy/scipy pueden lanzar casi cualquier cosa)
        res = {"ok": False, "resultado": f"Error en el cálculo: {type(e).__name__}: {str(e)[:500]}"}
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
