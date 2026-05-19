"""
Local Knowledge Base — CSE fundamentals.
Used when ALL LLM models are rate-limited.
Covers common BSc CSE topics that students ask about.
Pattern matching → instant answer, zero API calls.
"""

import re
from typing import Optional

# ─── KNOWLEDGE BASE ────────────────────────────────────────────────────────────
# Format: list of (patterns, answer)
# Patterns are regex strings matched against lowercased query

KB: list = [

    # ── OS ─────────────────────────────────────────────────────────────────────
    (["deadlock", "dead lock"],
    "Deadlock is a situation where two or more processes are stuck waiting for each other forever, and none can proceed.\n\n"
    "Example: Process A holds Resource 1 and waits for Resource 2. Process B holds Resource 2 and waits for Resource 1. Both wait forever.\n\n"
    "Four necessary conditions (Coffman conditions):\n"
    "1. Mutual Exclusion — resource held by one process at a time\n"
    "2. Hold and Wait — process holds one resource while waiting for another\n"
    "3. No Preemption — resource cannot be forcibly taken\n"
    "4. Circular Wait — circular chain of waiting processes\n\n"
    "Prevention: break any one of these four conditions.\n"
    "Detection: resource allocation graph or Banker's algorithm.\n\n"
    "Search @drcrow_bot for related files."),

    (["process", "thread", "difference between process and thread"],
    "Process: an independent program in execution with its own memory space.\n"
    "Thread: a lightweight unit of execution within a process, shares memory with other threads.\n\n"
    "Key differences:\n"
    "- Processes are isolated; threads share the same address space\n"
    "- Context switching between processes is slower than between threads\n"
    "- A crash in one process doesn't affect others; a thread crash can kill the whole process\n"
    "- Threads communicate via shared memory; processes use IPC (pipes, sockets)\n\n"
    "Search @drcrow_bot for related files."),

    (["scheduling", "cpu scheduling", "fcfs", "sjf", "round robin", "priority scheduling"],
    "CPU Scheduling algorithms decide which process runs next.\n\n"
    "Common algorithms:\n"
    "FCFS (First Come First Serve) — simple, non-preemptive, convoy effect problem\n"
    "SJF (Shortest Job First) — optimal average wait time, needs burst time prediction\n"
    "Round Robin — preemptive, uses time quantum, good for time-sharing systems\n"
    "Priority Scheduling — each process has priority, starvation is a risk\n"
    "MLFQ (Multi-Level Feedback Queue) — combines multiple queues with different priorities\n\n"
    "Search @drcrow_bot for related files."),

    (["paging", "page", "page table", "page fault"],
    "Paging is a memory management technique that eliminates external fragmentation.\n\n"
    "How it works:\n"
    "- Physical memory divided into fixed-size frames\n"
    "- Logical memory divided into same-size pages\n"
    "- Page table maps logical pages to physical frames\n"
    "- Page fault occurs when a page is not in memory — OS loads it from disk\n\n"
    "TLB (Translation Lookaside Buffer) caches recent page table entries for speed.\n\n"
    "Search @drcrow_bot for related files."),

    (["semaphore", "mutex", "monitor"],
    "These are synchronization tools to prevent race conditions.\n\n"
    "Mutex: a lock — only one thread can hold it at a time. Binary (0 or 1).\n\n"
    "Semaphore: a counter — can allow N threads simultaneously.\n"
    "wait(S): S-- (if S<0, block)\n"
    "signal(S): S++ (wake up a blocked process)\n\n"
    "Monitor: high-level construct with mutual exclusion built in. Condition variables used for waiting.\n\n"
    "Key difference: mutex has ownership (only locker can unlock), semaphore does not.\n\n"
    "Search @drcrow_bot for related files."),

    # ── DBMS ───────────────────────────────────────────────────────────────────
    (["normalization", "1nf", "2nf", "3nf", "bcnf", "normal form"],
    "Normalization removes redundancy and improves data integrity.\n\n"
    "1NF: atomic values only, no repeating groups\n"
    "2NF: 1NF + no partial dependency (non-key attributes depend on full primary key)\n"
    "3NF: 2NF + no transitive dependency (non-key attributes depend only on primary key)\n"
    "BCNF: stronger 3NF — every determinant must be a candidate key\n\n"
    "Rule of thumb: go to 3NF for most practical databases, BCNF when lossless decomposition is possible.\n\n"
    "Search @drcrow_bot for related files."),

    (["acid", "transaction", "atomicity", "consistency", "isolation", "durability"],
    "ACID properties guarantee reliable database transactions.\n\n"
    "Atomicity: transaction is all-or-nothing. If any part fails, everything rolls back.\n"
    "Consistency: database moves from one valid state to another valid state.\n"
    "Isolation: concurrent transactions don't interfere with each other.\n"
    "Durability: committed transactions survive system failures (stored to disk).\n\n"
    "Search @drcrow_bot for related files."),

    (["sql", "join", "inner join", "left join", "right join", "outer join"],
    "SQL JOINs combine rows from two or more tables.\n\n"
    "INNER JOIN: returns rows that have matching values in both tables.\n"
    "LEFT JOIN: all rows from left table + matching rows from right (NULL if no match).\n"
    "RIGHT JOIN: all rows from right table + matching rows from left.\n"
    "FULL OUTER JOIN: all rows from both tables, NULL where no match.\n"
    "CROSS JOIN: cartesian product of both tables.\n\n"
    "Example: SELECT * FROM students INNER JOIN courses ON students.course_id = courses.id\n\n"
    "Search @drcrow_bot for related files."),

    (["index", "b tree", "b+ tree", "database index"],
    "A database index speeds up data retrieval at the cost of extra storage.\n\n"
    "B-Tree: balanced tree, data stored in all nodes, good for range queries.\n"
    "B+ Tree: data only in leaf nodes, leaves linked — most databases use this.\n"
    "Hash Index: O(1) lookup but only for equality, no range queries.\n\n"
    "When to index: columns used in WHERE, JOIN, ORDER BY frequently.\n"
    "When NOT to index: small tables, columns with low cardinality.\n\n"
    "Search @drcrow_bot for related files."),

    # ── DSA ────────────────────────────────────────────────────────────────────
    (["big o", "time complexity", "space complexity", "complexity"],
    "Big-O notation describes algorithm efficiency as input size grows.\n\n"
    "Common complexities (best to worst):\n"
    "O(1) — constant: array access\n"
    "O(log n) — logarithmic: binary search\n"
    "O(n) — linear: linear search\n"
    "O(n log n) — merge sort, heap sort\n"
    "O(n^2) — quadratic: bubble sort, insertion sort\n"
    "O(2^n) — exponential: recursive fibonacci\n"
    "O(n!) — factorial: brute force travelling salesman\n\n"
    "Search @drcrow_bot for related files."),

    (["sorting", "bubble sort", "merge sort", "quick sort", "heap sort", "insertion sort"],
    "Common sorting algorithms:\n\n"
    "Bubble Sort: O(n^2), stable, swaps adjacent elements. Simple but slow.\n"
    "Insertion Sort: O(n^2), stable, efficient for nearly sorted data.\n"
    "Merge Sort: O(n log n), stable, divide and conquer. Uses extra space.\n"
    "Quick Sort: O(n log n) average, O(n^2) worst, in-place, fastest in practice.\n"
    "Heap Sort: O(n log n), not stable, in-place.\n\n"
    "For most cases: use Quick Sort (fastest average). For stability needed: use Merge Sort.\n\n"
    "Search @drcrow_bot for related files."),

    (["linked list", "singly linked", "doubly linked", "circular linked"],
    "A linked list is a linear data structure where each node points to the next.\n\n"
    "Singly Linked: each node has data + next pointer. Traverse forward only.\n"
    "Doubly Linked: each node has data + next + prev pointer. Traverse both ways.\n"
    "Circular: last node points back to first. No null at end.\n\n"
    "Operations:\n"
    "Insert at head: O(1)\n"
    "Insert at tail: O(n) singly, O(1) doubly with tail pointer\n"
    "Search: O(n)\n"
    "Delete: O(1) if node given, O(n) if searching by value\n\n"
    "Search @drcrow_bot for related files."),

    (["stack", "queue", "deque"],
    "Stack: LIFO (Last In First Out). Operations: push, pop, peek. All O(1).\n"
    "Uses: function call stack, undo operations, expression evaluation.\n\n"
    "Queue: FIFO (First In First Out). Operations: enqueue, dequeue. All O(1).\n"
    "Uses: BFS, task scheduling, print queue.\n\n"
    "Deque (Double-Ended Queue): insert/delete from both ends. O(1).\n"
    "Uses: sliding window problems, palindrome check.\n\n"
    "Search @drcrow_bot for related files."),

    (["tree", "binary tree", "bst", "binary search tree", "avl", "red black"],
    "Binary Tree: each node has at most 2 children (left, right).\n\n"
    "BST (Binary Search Tree): left < root < right. Search O(h) where h is height.\n"
    "Balanced BST: height O(log n) guaranteed.\n\n"
    "AVL Tree: self-balancing BST. Height difference between subtrees <= 1. Rotations maintain balance.\n"
    "Red-Black Tree: self-balancing, less strict than AVL, faster insertions. Used in Java TreeMap.\n\n"
    "Traversals: Inorder (LNR), Preorder (NLR), Postorder (LRN), Level-order (BFS).\n\n"
    "Search @drcrow_bot for related files."),

    (["graph", "bfs", "dfs", "dijkstra", "shortest path"],
    "Graph: nodes (vertices) connected by edges. Can be directed/undirected, weighted/unweighted.\n\n"
    "BFS (Breadth First Search): explores level by level using a queue. O(V+E).\n"
    "Use: shortest path in unweighted graph.\n\n"
    "DFS (Depth First Search): explores as deep as possible using stack/recursion. O(V+E).\n"
    "Use: cycle detection, topological sort, connected components.\n\n"
    "Dijkstra: shortest path in weighted graph (no negative weights). O((V+E) log V) with priority queue.\n\n"
    "Search @drcrow_bot for related files."),

    # ── OOP ────────────────────────────────────────────────────────────────────
    (["oop", "object oriented", "encapsulation", "inheritance", "polymorphism", "abstraction"],
    "OOP is a paradigm based on objects containing data and behavior.\n\n"
    "Four pillars:\n"
    "Encapsulation: bundling data and methods, hiding internal state. Use getters/setters.\n"
    "Inheritance: child class inherits properties of parent. Promotes code reuse.\n"
    "Polymorphism: same interface, different behavior. Method overriding and overloading.\n"
    "Abstraction: hiding complex implementation, showing only what's necessary. Abstract classes, interfaces.\n\n"
    "Search @drcrow_bot for related files."),

    # ── NETWORKING ─────────────────────────────────────────────────────────────
    (["tcp", "udp", "tcp vs udp", "transmission control"],
    "TCP (Transmission Control Protocol):\n"
    "Connection-oriented, reliable, ordered delivery.\n"
    "Uses handshake (SYN, SYN-ACK, ACK). Has flow control and congestion control.\n"
    "Slower but guaranteed delivery. Use for: HTTP, email, file transfer.\n\n"
    "UDP (User Datagram Protocol):\n"
    "Connectionless, unreliable, no ordering guarantee.\n"
    "No handshake, no retransmission. Faster.\n"
    "Use for: video streaming, gaming, DNS, VoIP.\n\n"
    "Search @drcrow_bot for related files."),

    (["osi", "osi model", "tcp/ip model", "network layer"],
    "OSI Model has 7 layers (top to bottom):\n"
    "7. Application — HTTP, FTP, SMTP\n"
    "6. Presentation — encryption, compression\n"
    "5. Session — session management\n"
    "4. Transport — TCP, UDP, port numbers\n"
    "3. Network — IP, routing\n"
    "2. Data Link — MAC address, Ethernet\n"
    "1. Physical — cables, signals\n\n"
    "Memory trick: All People Seem To Need Data Processing (top to bottom)\n\n"
    "Search @drcrow_bot for related files."),

    # ── COMPUTER ARCHITECTURE ──────────────────────────────────────────────────
    (["cache", "cache memory", "l1 l2 l3", "cache hit", "cache miss"],
    "Cache is fast memory between CPU and RAM to speed up data access.\n\n"
    "Levels: L1 (fastest, smallest, inside CPU) → L2 → L3 (slowest, largest)\n"
    "Cache hit: data found in cache. Fast.\n"
    "Cache miss: data not in cache, must fetch from RAM. Slow.\n\n"
    "Mapping policies: Direct, Associative, Set-Associative.\n"
    "Replacement policies: LRU (Least Recently Used), FIFO, Random.\n\n"
    "Write policies: Write-through (update cache and RAM simultaneously), Write-back (update RAM only when evicted).\n\n"
    "Search @drcrow_bot for related files."),

    # ── COMPILER ───────────────────────────────────────────────────────────────
    (["compiler", "interpreter", "lexer", "parser", "lexical analysis", "syntax analysis"],
    "Compiler phases:\n"
    "1. Lexical Analysis (Lexer/Scanner): source code → tokens\n"
    "2. Syntax Analysis (Parser): tokens → parse tree (checks grammar)\n"
    "3. Semantic Analysis: type checking, scope resolution\n"
    "4. Intermediate Code Generation\n"
    "5. Optimization\n"
    "6. Code Generation: → machine code\n\n"
    "Compiler: translates entire program before execution (C, C++, Java).\n"
    "Interpreter: translates and executes line by line (Python, JavaScript).\n\n"
    "Search @drcrow_bot for related files."),

    # ── GENERAL ────────────────────────────────────────────────────────────────
    (["recursion", "recursive"],
    "Recursion is when a function calls itself to solve a smaller version of the same problem.\n\n"
    "Every recursive function needs:\n"
    "1. Base case — condition to stop recursion\n"
    "2. Recursive case — function calls itself with smaller input\n\n"
    "Example (factorial):\n"
    "fact(n) = 1 if n==0, else n * fact(n-1)\n\n"
    "Tail recursion: recursive call is the last operation — compilers can optimize it.\n"
    "Stack overflow: too many recursive calls without hitting base case.\n\n"
    "Search @drcrow_bot for related files."),

    (["dynamic programming", "dp", "memoization", "tabulation"],
    "Dynamic Programming solves complex problems by breaking them into overlapping subproblems.\n\n"
    "Two approaches:\n"
    "Memoization (top-down): recursion + caching results to avoid recomputation.\n"
    "Tabulation (bottom-up): fill a table iteratively from smallest subproblem.\n\n"
    "Classic DP problems: Fibonacci, Knapsack, Longest Common Subsequence, Coin Change, Matrix Chain Multiplication.\n\n"
    "When to use DP: optimal substructure + overlapping subproblems.\n\n"
    "Search @drcrow_bot for related files."),
]


def get_local_answer(query: str) -> Optional[str]:
    """
    Try to match query against local KB.
    Returns answer string or None if no match.
    """
    q = query.lower().strip()

    for patterns, answer in KB:
        for pattern in patterns:
            if re.search(pattern, q):
                return answer

    return None