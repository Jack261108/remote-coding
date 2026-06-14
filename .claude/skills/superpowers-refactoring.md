---
name: superpowers:refactoring
description: Disciplined refactoring skill that starts by understanding current behavior, preserves functionality, and uses tests as guardrails. Follows the Boy Scout Rule - leave code better than you found it.
license: MIT
---

# Refactoring Skill

You are about to refactor code. This is a disciplined process that prioritizes understanding before change, behavior preservation, and incremental improvement.

## Context

This skill applies when:
- Improving existing code structure without changing behavior
- Cleaning up technical debt
- Applying design patterns to existing code
- Modernizing deprecated APIs or patterns
- Improving readability, maintainability, or performance

This skill does NOT apply when:
- Adding new features (use appropriate feature skill)
- Fixing bugs (use debugging workflow)
- The user explicitly wants to change behavior

## Instructions

### Phase 1: Understand Current Behavior

Before making ANY changes:

1. **Read the code thoroughly**
   - Understand what it does, not how it's structured
   - Identify inputs, outputs, and side effects
   - Note any external dependencies or integrations

2. **Identify the test suite**
   - Locate existing tests for the code being refactored
   - If no tests exist, STOP and ask: "Should we add tests first?"
   - Tests are your safety net - never refactor without them

3. **Document current behavior**
   - Summarize what the code does in plain language
   - Note any edge cases or quirks
   - Identify the public API (what callers depend on)

4. **Check for assumptions**
   - Don't assume you know what the code does
   - Verify by reading tests, comments, and calling code
   - Ask if anything is unclear

### Phase 2: Plan the Refactoring

1. **Identify refactoring goals**
   - What specifically needs improvement?
   - Why is the current structure problematic?
   - What will the new structure provide?

2. **Choose refactoring technique**
   - Extract Method/Function
   - Rename for clarity
   - Replace with design pattern
   - Simplify conditional logic
   - Remove duplication
   - Improve naming

3. **Plan incremental steps**
   - Break refactoring into small, testable steps
   - Each step should pass all tests
   - Never refactor in large batches

4. **Identify risks**
   - What could break?
   - What callers might be affected?
   - Are there performance implications?

### Phase 3: Execute with Guardrails

1. **Ensure tests pass before starting**
   - Run the full test suite
   - Fix any failing tests first
   - This is your baseline

2. **Make one change at a time**
   - Single responsibility per commit
   - Run tests after each change
   - If tests fail, revert and reconsider

3. **Preserve behavior exactly**
   - Same inputs must produce same outputs
   - Same side effects must occur
   - Same error conditions must be handled

4. **Update tests if needed**
   - Tests may need updating for new structure
   - Never delete tests without explicit approval
   - Add tests for newly discovered edge cases

### Phase 4: Verify and Document

1. **Run full test suite**
   - All tests must pass
   - No regressions allowed
   - Performance tests if applicable

2. **Review the changes**
   - Compare before/after behavior
   - Ensure no unintended changes
   - Verify public API preserved

3. **Document what changed**
   - Why was refactoring needed?
   - What improved?
   - Any trade-offs made?

## Key Principles

### Boy Scout Rule
Leave the code better than you found it. If you're working in an area and see something that could be improved, improve it - but only if it's directly related to your current task.

### Behavior Preservation
The cardinal rule of refactoring: **never change behavior**. If you need to change behavior, that's a feature change, not refactoring.

### Test-Driven Refactoring
Tests are your safety net. If tests don't exist, consider adding them before refactoring. If you can't add tests, proceed with extreme caution and document risks.

### Incremental Changes
Small, frequent changes are safer than large, infrequent ones. Each change should be atomic and testable.

### Learn Before Assuming
Every project has its own patterns, conventions, and constraints. Learn the existing patterns before imposing your own. Don't assume you know better without evidence.

## Common Refactoring Patterns

### Extract Method/Function
When a function is too long or does too much:
1. Identify logical blocks of code
2. Extract into well-named functions
3. Ensure extracted function has clear inputs/outputs
4. Update tests to cover new functions

### Rename for Clarity
When names don't reveal intent:
1. Choose names that describe purpose, not implementation
2. Follow project naming conventions
3. Update all references
4. Verify no naming conflicts

### Replace with Design Pattern
When code follows a recognizable pattern:
1. Identify the pattern being used implicitly
2. Refactor to explicit pattern implementation
3. Ensure all existing behavior preserved
4. Document the pattern for future maintainers

### Simplify Conditional Logic
When conditionals are complex or nested:
1. Extract conditions into well-named functions
2. Use early returns to reduce nesting
3. Consider polymorphism for type-based conditions
4. Preserve all original conditions and branches

### Remove Duplication
When similar code appears in multiple places:
1. Identify the common pattern
2. Extract into shared function or base class
3. Parameterize the differences
4. Verify all original behavior preserved

## Error Handling

### If tests fail during refactoring:
1. **STOP immediately**
2. Revert the last change
3. Analyze why tests failed
4. Adjust refactoring approach
5. Never proceed with failing tests

### If you discover behavior that needs changing:
1. **STOP the refactoring**
2. Document the desired behavior change
3. Ask for explicit approval
4. Create separate task for behavior change
5. Continue refactoring only after approval

### If refactoring reveals bugs:
1. **Note the bug but don't fix it during refactoring**
2. Document the bug separately
3. Continue refactoring with behavior preservation
4. Create separate bug fix task

### If you're unsure about a change:
1. **Ask before proceeding**
2. Explain your uncertainty
3. Present alternatives
4. Let the human decide

## Examples

### Example 1: Extracting a Method

**Before:**
```python
def process_order(order):
    # Validate order
    if not order.items:
        raise ValueError("Order has no items")
    if order.total < 0:
        raise ValueError("Order total is negative")
    
    # Calculate discount
    discount = 0
    if order.customer.is_vip:
        discount = order.total * 0.1
    elif order.total > 100:
        discount = order.total * 0.05
    
    # Apply discount
    final_total = order.total - discount
    
    # Process payment
    payment_gateway.charge(order.customer, final_total)
    
    # Send confirmation
    email_service.send_confirmation(order.customer.email, order)
    
    return final_total
```

**After:**
```python
def process_order(order):
    validate_order(order)
    discount = calculate_discount(order)
    final_total = order.total - discount
    payment_gateway.charge(order.customer, final_total)
    email_service.send_confirmation(order.customer.email, order)
    return final_total

def validate_order(order):
    if not order.items:
        raise ValueError("Order has no items")
    if order.total < 0:
        raise ValueError("Order total is negative")

def calculate_discount(order):
    if order.customer.is_vip:
        return order.total * 0.1
    elif order.total > 100:
        return order.total * 0.05
    return 0
```

### Example 2: Renaming for Clarity

**Before:**
```python
def handle_data(d):
    p = parse(d)
    v = validate(p)
    if v:
        return save(p)
    return None
```

**After:**
```python
def process_user_input(raw_input):
    parsed_data = parse_input(raw_input)
    if is_valid(parsed_data):
        return persist_to_database(parsed_data)
    return None
```

### Example 3: Simplifying Conditionals

**Before:**
```python
def get_shipping_cost(order):
    if order.country == "US":
        if order.total > 100:
            return 0
        else:
            return 5.99
    elif order.country == "CA":
        if order.total > 200:
            return 0
        else:
            return 12.99
    else:
        if order.total > 500:
            return 0
        else:
            return 25.99
```

**After:**
```python
SHIPPING_RATES = {
    "US": {"threshold": 100, "cost": 5.99},
    "CA": {"threshold": 200, "cost": 12.99},
    "DEFAULT": {"threshold": 500, "cost": 25.99},
}

def get_shipping_cost(order):
    rate = SHIPPING_RATES.get(order.country, SHIPPING_RATES["DEFAULT"])
    if order.total > rate["threshold"]:
        return 0
    return rate["cost"]
```

## When to Stop Refactoring

Refactoring should stop when:
1. **Tests pass** and behavior is preserved
2. **Code is clear** enough that others can understand it
3. **Further changes** would be speculative or premature
4. **Time constraints** require moving on
5. **Diminishing returns** - effort exceeds benefit

Remember: Perfect is the enemy of good. Refactoring should improve code, not make it perfect.

## Integration with Other Skills

This refactoring skill works with:
- **Testing skills**: For adding or updating tests
- **Debugging skills**: When refactoring reveals bugs
- **Code review skills**: For reviewing refactored code
- **Documentation skills**: For updating docs after refactoring

Always check if other skills are more appropriate for your current task.
