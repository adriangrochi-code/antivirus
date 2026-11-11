% Plantilla TISMA: viga en voladizo con carga puntual en el extremo (unidades SI).
% Comprobación a mano: flecha = F*L^3/(3*E*I); tensión máx = F*L*c/I.
% Con F = 500 N, L = 1 m, sección rectangular 40x60 mm, acero E = 200 GPa:
%   I = b*h^3/12 = 0.04*0.06^3/12 = 7.2e-7 m^4 ; flecha = 1.157 mm ; sigma = 20.83 MPa
F = 500; L = 1.0; b = 0.04; h = 0.06; E = 200e9; Sy = 250e6;
I = b*h^3/12; c = h/2;
x = linspace(0, L, 201);
y = F*x.^2 .* (3*L - x) / (6*E*I);          % flecha a lo largo de la viga
sigma = F*L*c/I;
printf("I = %.4e m^4\n", I);
printf("flecha_max_mm = %.4f\n", 1e3*y(end));
printf("sigma_max_MPa = %.3f\n", sigma/1e6);
printf("factor_seguridad = %.2f\n", Sy/sigma);
