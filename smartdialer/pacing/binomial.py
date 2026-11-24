import math


def tail_gt(n: int, p: float, k: int) -> float:
    """P(X > k) for X ~ Binomial(n, p). Exact for small k, normal approximation otherwise."""
    if k < 0:
        return 1.0
    if k >= n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0 if n > k else 0.0
    if k <= 600 or n * p * (1 - p) < 25:
        lp, lq = math.log(p), math.log1p(-p)
        lg = math.lgamma
        cdf = 0.0
        for i in range(0, k + 1):
            cdf += math.exp(lg(n + 1) - lg(i + 1) - lg(n - i + 1) + i * lp + (n - i) * lq)
        return max(0.0, 1.0 - cdf)
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    z = (k + 0.5 - mu) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))


def max_total_calls(capacity: int, p: float, eps: float, limit: int) -> int:
    """Largest T <= limit with P(Binomial(T, p) > capacity) <= eps. The tail is monotone in T."""
    if capacity <= 0:
        return 0
    lo, hi = capacity, max(capacity, limit)
    if tail_gt(hi, p, capacity) <= eps:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tail_gt(mid, p, capacity) <= eps:
            lo = mid
        else:
            hi = mid - 1
    return lo
