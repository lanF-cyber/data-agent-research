SELECT GovernmentForm, AVG(LifeExpectancy) AS AverageLifeExpectancy FROM country GROUP BY GovernmentForm;
SELECT GovernmentForm, AVG(LifeExpectancy) AS AverageLifeExpectancy FROM country GROUP BY GovernmentForm HAVING AVG(LifeExpectancy) > 72;
SELECT GovernmentForm, SUM(Population) AS Population FROM country GROUP BY GovernmentForm HAVING AVG(LifeExpectancy) > 72;